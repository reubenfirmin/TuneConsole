"""Cleanup progresses through history larger than one SQL deadline."""
from pathlib import Path
import time

import pytest

from yt_playlist.core import action_backup_inventory, collection_recovery as recovery, collection_recovery_epoch
from yt_playlist.controllers.collection_retention import CollectionCollector
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.store import Store
from tests.genre_catalog_support import bound_catalog


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These collector owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('collection-scale-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield




@pytest.fixture(scope='module')
def owner_schema(_pristine_current_schema):
    return _pristine_current_schema


def test_scale_schema_has_real_current_action_constraints_and_recovery_epoch(owner_schema):
    assert owner_schema.execute("SELECT 1 FROM sqlite_schema WHERE name='action_identity'").fetchone()
    assert owner_schema.execute("SELECT 1 FROM schema_changes WHERE name='collection_recovery_scan_keys'").fetchone()


@pytest.fixture(scope='module')
def owner_history(owner_schema, tmp_path_factory):
    # Keep the 400,000 unindexed owners with the primary key, recovery filename,
    # and backup inventory that the collector reads.
    # Native writer/admission and ownership invalidation have full-schema owners.
    store = Store(tmp_path_factory.mktemp('collection-owner-history') / 'installed.db')
    try:
        owner_schema.backup(store.conn)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store.conn.execute('PRAGMA synchronous=OFF')
        with store.conn:
            store.conn.execute('DROP TABLE actions')
            store.conn.execute('CREATE TABLE actions(id INTEGER PRIMARY KEY,recovery_filename TEXT NOT NULL,'
                               f'{action_backup_inventory.DECLARATION})')
            store.conn.execute('''WITH d(x) AS (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)),
                high(x) AS (VALUES(0),(1),(2),(3)),
                n(x) AS (SELECT 1+a.x*100000+b.x*10000+c.x*1000+d.x*100+e.x*10+f.x
                    FROM high a CROSS JOIN d b CROSS JOIN d c CROSS JOIN d CROSS JOIN d e CROSS JOIN d f)
                INSERT INTO actions (id,recovery_filename) SELECT x,?||printf('/ab/%064d.json',x) FROM n''',
                (str(recovery.directory(store.conn)),))
            # Restore the exact production ownership and key-change triggers on
            # the projected owner; unrelated external commits must not restart it.
            collection_recovery_epoch.install(store.conn)
            collection_recovery_epoch.install_keys(store.conn)
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        yield store
    finally:
        store.retention.collections.close()
        store.conn.close()


def test_completed_scale_input_has_all_400000_private_unindexed_owners(owner_history):
    conn = owner_history.conn
    assert tuple(conn.execute('SELECT count(*),min(id),max(id) FROM actions').fetchone()) == (400000, 1, 400000)
    assert not conn.execute('SELECT 1 FROM actions WHERE recovery_filename<>?||printf(\'/ab/%064d.json\',id) LIMIT 1',
                            (str(recovery.directory(conn)),)).fetchone()
    assert not conn.execute('PRAGMA index_list(actions)').fetchone()
    assert not any(recovery.directory(conn).rglob('*.json'))
    collection_recovery_epoch.validate(conn)
    collection_recovery_epoch.validate_keys(conn)


def test_native_collector_admission_preserves_the_real_current_owner_epoch(file_backed_store):
    store = file_backed_store
    revision = collection_recovery_epoch.revision(store.conn)
    writer = Store(store._db_path)
    try:
        writer.init_schema()
        writer.settings.set_setting('accepted after fresh admission', 'keep')
        assert store.settings.get_setting('accepted after fresh admission') == 'keep'
        assert collection_recovery_epoch.revision(writer.conn) == revision
        collection_recovery_epoch.validate(writer.conn)
        collection_recovery_epoch.validate_keys(writer.conn)
    finally:
        writer.conn.close()


@pytest.mark.parametrize('external', [False, True])
def test_large_unindexed_owner_history_makes_progress(owner_history, record_property, external):
    store = owner_history
    started = time.perf_counter()
    orphan = Path(recovery.publish(store.conn, 'action', f'unowned-{external}', {'orphan': True})[0])
    record_property('orphan_publication_seconds', time.perf_counter() - started)
    turns = []
    writer = Store(store._db_path) if external else store
    try:
        if external:
            writer._bind_repositories()
            # This private collector fixture checks commit visibility and
            # invalidation, independently of power-loss durability.
            writer.conn.execute('PRAGMA synchronous=NORMAL')
        for turn in range(12):
            started = time.monotonic()
            store.retention.collections.maintain()
            turns.append(time.monotonic() - started)
            assert not store.conn.in_transaction
            writer.settings.set_setting('between scale cleanup turns', str(turn))
            if not orphan.exists():
                break
        assert not orphan.exists(), 'Cleanup must retire a batch across bounded turns'
        record_property('owner_count', 400000)
        record_property('turn_count', len(turns))
        record_property('collector_total_seconds', sum(turns))
        record_property('maximum_turn_seconds', max(turns))
        assert CollectionCollector.BUDGET_S == .05
    finally:
        if external:
            writer.conn.close()
        store.retention.collections.close()


@pytest.mark.parametrize('external', [False, True])
def test_new_reference_invalidates_partial_scan(file_backed_store, monkeypatch, external):
    store = file_backed_store
    for index in range(4):
        store.playlist_recipes.set_recipe(f'earlier-{index}', {'value': index}, 1)
    filename, digest, size = recovery.publish(store.conn, 'playlist-recipe', 1, {'keep': True})
    orphan = Path(filename)
    monkeypatch.setattr(CollectionCollector, 'OWNER_PAGE', 2)
    from yt_playlist.repos import collection_retention
    check = collection_retention.check_cancellable_work

    def interrupt_after_first_page():
        collector = store.retention.database._collection_orphans
        if (collector.owners._table_index < len(collection_retention.FILE_OWNERS)
                and collection_retention.FILE_OWNERS[collector.owners._table_index][1] == 'recipe_recovery_filename'
                and collector.owners._after is not None):
            raise WorkBudgetExceeded('Pause after a completed owner page')
        check()

    with monkeypatch.context() as interrupted:
        interrupted.setattr(collection_retention, 'check_cancellable_work', interrupt_after_first_page)
        for _ in range(30):
            store.retention.collections.maintain()
            collector = store.retention.database._collection_orphans
            if collector.owners._after is not None:
                break
        else:
            pytest.fail('Did not reach the partial owner scan')
    assert orphan.exists()
    writer = Store(store._db_path) if external else store
    try:
        # Update behind the saved keyset position. Resuming without validating
        # ownership changes would skip this newly published current reference.
        with writer.conn:
            writer.conn.execute('''UPDATE playlist_scopes SET recipe_revision=?,
                recipe_recovery_filename=?,recipe_recovery_digest=?,recipe_recovery_bytes=? WHERE recipe_id=1''',
                ('1' * 32, filename, digest, size))
        for _ in range(100):
            store.retention.collections.maintain()
            if not store.retention.collections.pending():
                break
        assert not store.retention.collections.pending()
        assert orphan.exists()
    finally:
        if external:
            writer.conn.close()
        store.retention.collections.close()
