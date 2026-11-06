"""Cleanup progresses through history larger than one SQL deadline."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time

import pytest

from yt_playlist.core import collection_recovery as recovery, policy_documents
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.store import Store
from tests.store_fixture_support import initialize_consumer


def seed_owner_history(store):
    # SQL-only scale history, using real current constraints. These coordinates
    # deliberately do not claim to be readable recovery documents.
    store.conn.execute('''WITH RECURSIVE n(x) AS (
        VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<400000)
        INSERT INTO actions(id,kind,status,revision,idempotency_key,intent_digest,
                            recovery_filename,recovery_digest,recovery_bytes)
        SELECT x,'scale','executed',?,printf('scale-%d',x),?,
               ?||printf('/ab/%064d.json',x),?,128 FROM n''',
        ('0' * 32, '0' * 64, str(recovery.directory(store.conn)), '0' * 64))
    store.conn.commit()


@pytest.fixture(scope='module')
def owner_history_template(tmp_path_factory, _pristine_current_schema):
    # Keep the large immutable prefix on disk rather than retaining another
    # complete Python filename inventory or an in-memory SQL database.
    with TemporaryDirectory(prefix='collection-owner-history-', dir=tmp_path_factory.getbasetemp()) as directory:
        root = Path(directory)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(root / 'app-home'))
            source = Store(root / 'source.db')
            try:
                initialize_consumer(source, _pristine_current_schema)
                started = time.perf_counter()
                seed_owner_history(source)
                build_seconds = time.perf_counter() - started
                # The prefix owns no readable native files. Actual orphans are
                # created independently after each private restore.
                assert not any(recovery.directory(source.conn).rglob('*.json'))
                source.conn.execute('PRAGMA query_only=ON')
            except BaseException:
                source.conn.close()
                raise
        try:
            yield source, build_seconds
        finally:
            source.conn.close()


@pytest.fixture
def owner_history(file_backed_store, owner_history_template, record_property):
    store = file_backed_store
    source, build_seconds = owner_history_template
    started = time.perf_counter()
    source.conn.backup(store.conn)
    record_property('history_backup_seconds', time.perf_counter() - started)
    started = time.perf_counter()
    # These are deliberately unreadable fixture coordinates, not retained
    # documents to export. Preserve every exact suffix with one set operation.
    store.conn.execute('BEGIN')
    with store.conn, policy_documents.coordinate_updates(store.conn, ('actions',)):
        changed = store.conn.execute('UPDATE actions SET recovery_filename=?||substr(recovery_filename,?)',
            (str(recovery.directory(store.conn)), len(str(recovery.directory(source.conn))) + 1))
        assert changed.rowcount == 400000
    record_property('history_relocation_seconds', time.perf_counter() - started)
    record_property('history_cold_build_seconds', build_seconds)
    record_property('history_template_id', str(source._db_path))
    assert store.conn.execute('SELECT COUNT(*) FROM actions').fetchone()[0] == 400000
    assert not store.conn.execute('SELECT 1 FROM actions WHERE recovery_filename<>?||printf(\'/ab/%064d.json\',id) LIMIT 1',
                                  (str(recovery.directory(store.conn)),)).fetchone()
    return store


@pytest.mark.parametrize('external', [False, True])
def test_large_unindexed_owner_history_makes_progress(owner_history, record_property, external):
    store = owner_history
    started = time.perf_counter()
    orphan = Path(recovery.publish(store.conn, 'action', 'unowned', {'orphan': True})[0])
    record_property('orphan_publication_seconds', time.perf_counter() - started)
    turns = []
    writer = Store(store._db_path) if external else store
    try:
        if external:
            started = time.perf_counter()
            writer.init_schema()
            record_property('independent_admission_seconds', time.perf_counter() - started)
        for turn in range(12):
            started = time.monotonic()
            recovery.maintain(store)
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
        assert recovery.OrphanCollector.BUDGET_S == .05
    finally:
        if external:
            writer.conn.close()
        recovery.close(store)


@pytest.mark.parametrize('external', [False, True])
def test_new_reference_invalidates_partial_scan(file_backed_store, monkeypatch, external):
    store = file_backed_store
    for index in range(4):
        store.playlist_recipes.set_recipe(f'earlier-{index}', {'value': index}, 1)
    filename, digest, size = recovery.publish(store.conn, 'playlist-recipe', 1, {'keep': True})
    orphan = Path(filename)
    monkeypatch.setattr(recovery.OrphanCollector, 'OWNER_PAGE', 2)
    check = recovery.check_cancellable_work

    def interrupt_after_first_page():
        collector = store._collection_orphans
        if (collector._table_index < len(collector._tables)
                and collector._tables[collector._table_index][0] == 'rec_recipes'
                and collector._after is not None):
            raise WorkBudgetExceeded('Pause after a completed owner page')
        check()

    with monkeypatch.context() as interrupted:
        interrupted.setattr(recovery, 'check_cancellable_work', interrupt_after_first_page)
        for _ in range(30):
            recovery.maintain(store)
            collector = store._collection_orphans
            if collector._after is not None:
                break
        else:
            pytest.fail('Did not reach the partial owner scan')
    assert orphan.exists()
    writer = Store(store._db_path) if external else store
    try:
        # Update behind the saved keyset position. Resuming without validating
        # ownership changes would skip this newly published current reference.
        with writer.conn:
            writer.conn.execute('''UPDATE rec_recipes SET revision=?,
                recovery_filename=?,recovery_digest=?,recovery_bytes=? WHERE id=1''',
                ('1' * 32, filename, digest, size))
        for _ in range(100):
            recovery.maintain(store)
            if not recovery.pending(store):
                break
        assert not recovery.pending(store)
        assert orphan.exists()
    finally:
        if external:
            writer.conn.close()
        recovery.close(store)
