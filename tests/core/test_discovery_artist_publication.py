"""Artist file selection must retain originals and leave old writers usable."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import threading

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.core.test_predecessor_writes import consumer_installed as unadmitted_installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_discovery_owner_preparation import seed
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import schema_inspection, collection_recovery, policy_documents
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import discovery_artists as preparation, discovery_artist_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication owners do not construct an HTTP application."""


@pytest.mark.parametrize('version', [79, 85, 86, 93, 94, 100])
def test_empty_publication_predecessor_keeps_pending_ddl_and_revision_guards(
        _bounded_installed_schema, version):
    template = _bounded_installed_schema(version)
    conn = template.source.conn
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert regular.validate(conn) == 0
    assert template.revision_guards
    assert template.source._snapshot_revisions.conn is conn
    assert not conn.execute('SELECT 1 FROM main.discovered_artists').fetchone()


def prepare(conn):
    # These are completed lower inputs for publication. Reuse the production
    # table-inspection session while real workers build them; all validators,
    # source copying and file writes still run.
    with schema_inspection.reuse_tables(conn):
        owner = publication.owner(conn)
        owner.worker.install(conn)
        for _ in range(300):
            if owner.worker.step(conn, batch_size=8).status == 'prepared':
                return owner
        pytest.fail('Discovery explanation fixture did not prepare')


def ready(conn, size=8):
    owner = prepare(conn)
    for _ in range(300):
        stage = owner.step(conn, batch_size=size)
        if stage.complete:
            return owner, stage.id
    pytest.fail('Discovery explanation fixture did not stage')


@pytest.mark.parametrize('unadmitted_installed', [79, 85, 86, 100], indirect=True)
def test_selected_files_keep_installed_identity_counters_genres_and_preopened_readers(unadmitted_installed, independent_regular_ddl):
    store, _ = unadmitted_installed
    _, aid, _, _ = seed(store)
    with store.conn:
        if publication.owner(store.conn).name == 'artists_legacy':
            store.conn.execute("UPDATE main.discovered_artists SET genre='Captured Unknown Genre'")
        else:
            store.conn.execute("UPDATE main.discovered_artists SET genre_key=NULL,unresolved_genre='Captured Unknown Genre'")
    expected = store.discovery.get_discovered_artists()
    originals = list(map(dict, store.conn.execute('SELECT * FROM main.discovered_artists ORDER BY artist')))
    history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        with other.read_snapshot() as snapshot:
            pinned = snapshot.store
            assert pinned.discovery.get_discovered_artists() == expected
            owner, stage = ready(store.conn)
            owner.publish(store.conn, stage=stage)
            assert owner.selected(pinned.conn) is None
            assert pinned.discovery.get_discovered_artists() == expected
        with other.read_view() as selected:
            def deny_old(action, table, column, *_):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'discovered_artists' else sqlite3.SQLITE_OK
            selected.conn.set_authorizer(deny_old)
            try:
                assert selected.discovery.get_discovered_artists() == expected
                assert selected.discovery.discovery_artist_view(100, 7)[0]['legacy_offered_count'] == 7
                assert selected.discovery.present_home_tiles([], [aid])[1][aid]['genre'] == 'Captured Unknown Genre'
                assert selected.discovery.artist_images_due([aid], 100) == [{'artist_id': aid, 'artist': 'Candidate'}]
            finally:
                selected.conn.set_authorizer(None)
        assert other.discovery.get_discovered_artists() == expected
        assert list(map(dict, store.conn.execute('SELECT * FROM main.discovered_artists ORDER BY artist'))) == originals
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        regular.migrate(other.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('damage', ['metadata', 'identity', 'file', 'part', 'plan', 'missing'])
def test_independent_verification_rejects_incorrect_files_metadata_and_coverage(installed, damage, independent_regular_ddl):
    store, _ = installed
    seed(store)
    owner = prepare(store.conn)
    header, parts = preparation.table(owner.name, 'generations'), preparation.table(owner.name, 'parts')
    with store.conn:
        if damage == 'metadata':
            store.conn.execute('UPDATE '+header+' SET offered_count=99')
        elif damage == 'identity':
            store.conn.execute('UPDATE '+header+" SET artist_id=NULL,artist_resolution='missing_canonical_artist'")
        elif damage == 'file':
            store.conn.execute('UPDATE '+header+" SET recovery_filename='/missing'")
        elif damage == 'part':
            store.conn.execute('UPDATE '+parts+" SET content_digest=printf('%064d',0)")
        elif damage == 'plan':
            store.conn.execute('UPDATE '+header+" SET plan_filename='/missing'")
        elif damage == 'missing':
            store.conn.execute('DELETE FROM '+owner.current)
    stage = owner.step(store.conn)
    assert stage.complete
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, stage=stage.id)
    assert owner.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_continuing_writes_retain_files_and_withdraw_selected_projection(installed):
    store, _ = installed
    seed(store)
    owner, first = ready(store.conn)
    owner.publish(store.conn, stage=first)
    source = store.conn.execute('SELECT artist FROM main.discovered_artists').fetchone()[0]
    original = owner.value(store.conn, first, source)
    old_header = owner.capture(store.conn, first, source)
    store.discovery.upsert_discovered_artist('Candidate', 5, ['New anchor'], ['New playlist'], 'new.jpg', 20)
    assert owner.selected(store.conn) is None
    assert store.discovery.get_discovered_artists()[0]['because'] == ['New anchor']
    owner, second = ready(store.conn)
    owner.publish(store.conn, stage=second)
    assert second != first
    assert store.discovery.get_discovered_artists()[0]['because'] == ['New anchor']
    assert owner.value(store.conn, first, source) == original
    assert collection_recovery.read(old_header['recovery_filename'], old_header['recovery_digest'], old_header['recovery_bytes'],
        kind='discovery-candidate', owner=old_header['artist_id']) == {'because': ['Anchor', 'Anchor'], 'fits': ['Playlist']}
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+owner.header+' WHERE source=?', (source,))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interruption_and_final_publication_race_keep_durable_checkpoint(installed, monkeypatch):
    store, _ = installed
    seed(store)
    store.discovery.upsert_discovered_artist('Second', 1, [], [], None, 2)
    owner = prepare(store.conn)
    first = owner.step(store.conn, batch_size=1)
    assert first.copied == 1 and not first.complete
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_artist_map_failure BEFORE INSERT ON '+owner.table('map')+
            " BEGIN SELECT RAISE(ABORT,'controlled artist interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled artist interruption'):
        owner.step(store.conn, batch_size=1)
    assert owner.stage(store.conn, first.id) == first
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_artist_map_failure')
    reopened = Store(store._db_path)
    try:
        stage = owner.step(reopened.conn, batch_size=1)
        assert stage.complete and stage.id == first.id and stage.copied == 2
        with reopened.conn:
            reopened.conn.execute('CREATE TRIGGER controlled_artist_head_failure BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled artist head interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled artist head interruption'):
            owner.publish(reopened.conn, stage=stage.id)
        assert not reopened.conn.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
        with reopened.conn:
            reopened.conn.execute('DROP TRIGGER controlled_artist_head_failure')
        verify = type(owner).verify
        def changed(self, conn, identity):
            proof = verify(self, conn, identity)
            with sqlite3.connect(store._db_path) as writer:
                writer.execute("UPDATE main.discovered_artists SET thumbnail='later.jpg'")
            return proof
        monkeypatch.setattr(type(owner), 'verify', changed)
        with pytest.raises(MigrationError, match='changed|reconciled'):
            owner.publish(reopened.conn, stage=stage.id)
        assert owner.selected(reopened.conn) is None
        monkeypatch.setattr(type(owner), 'verify', verify)
        owner, stage = ready(reopened.conn)
        owner.publish(reopened.conn, stage=stage)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_missing_files_regular_admission_and_explicit_unchanged_source_repair(installed, independent_regular_ddl):
    store, _ = installed
    seed(store)
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    header = store.conn.execute('SELECT * FROM '+owner.header).fetchone()
    Path(header['recovery_filename']).unlink()
    regular.migrate(store.conn)
    with pytest.raises(FileNotFoundError):
        store.discovery.get_discovered_artists()
    publication.repair(store.conn, header['source'])
    assert store.discovery.get_discovered_artists()[0]['because'] == ['Anchor', 'Anchor']
    assert owner.selected(store.conn) == stage
    with store.conn:
        store.conn.execute("UPDATE main.discovered_artists SET fits='[]'")
    with pytest.raises(MigrationError, match='changed'):
        publication.repair(store.conn, header['source'])


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_slow_verification_allows_real_writer_and_independent_ddl(installed, monkeypatch, independent_regular_ddl):
    store, _ = installed
    seed(store)
    owner, stage = ready(store.conn)
    entered, release = threading.Event(), threading.Event()
    read = collection_recovery.read
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return read(*args, **kwargs)
    monkeypatch.setattr(collection_recovery, 'read', slow)
    def publish():
        with sqlite3.connect(store._db_path, timeout=.1) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(MigrationError, match='changed|reconciled'):
                owner.publish(conn, stage=stage)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(publish)
        try:
            assert entered.wait(15)
            store.discovery.record_artist_image(store.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()[0], 'real.jpg', 100)
            regular.migrate(store.conn)
        finally:
            release.set()
        task.result(timeout=15)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_oversized_prepared_key_fails_before_map_writer_and_valid_original_is_retained(installed, independent_regular_ddl):
    store, _ = installed
    seed(store)
    owner = prepare(store.conn)
    good = store.conn.execute('SELECT * FROM '+owner.header).fetchone()
    original = preparation.read_original(store.conn, owner.name, good)
    owner.install(store.conn)
    # Challenge publication with corrupt preparation independently of the file
    # encoder's smaller, existing source-address limit. Keep the valid original.
    invalid = dict(good)
    invalid['source'] = 'x'*(publication.MAX_KEY_BYTES+1)
    with store.conn:
        store.conn.execute('INSERT INTO '+owner.header+'('+','.join(invalid)+') VALUES ('+
                          ','.join('?' for _ in invalid)+')', tuple(invalid.values()))
        store.conn.execute('INSERT INTO '+owner.current+' VALUES (?,?)',
                           (invalid['source'], invalid['preparation_revision']))
    sql = []
    store.conn.set_trace_callback(sql.append)
    try:
        with pytest.raises(MigrationError, match='key exceeds'):
            owner.step(store.conn)
    finally:
        store.conn.set_trace_callback(None)
    # Completed installation validates without a writer reservation. Invalid
    # preparation must also fail before map publication can reserve a writer.
    assert sql.count('BEGIN IMMEDIATE') == 0
    assert not store.conn.execute('SELECT 1 FROM '+owner.table('stages')).fetchone()
    assert preparation.read_original(store.conn, owner.name, good) == original
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [79, 100], indirect=True)
def test_native_upgrade_keeps_publication_custody_and_uses_native_owner(installed, tmp_path):
    store, _ = installed
    seed(store)
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    captured = list(map(tuple, store.conn.execute('SELECT * FROM '+owner.table('map'))))
    source = store.conn.execute('SELECT source FROM '+owner.header).fetchone()[0]
    original = owner.value(store.conn, stage, source)
    native = Store(tmp_path/'native-upgrade.db')
    try:
        store.conn.backup(native.conn)
        runner.migrate(native.conn, registry=registry.MIGRATIONS)
        native.init_schema(defer_metadata=True)
        assert owner.selected(native.conn) is None
        assert publication.relation(native.conn) == 'main.discovered_artists'
        assert list(map(tuple, native.conn.execute('SELECT * FROM '+owner.table('map')))) == captured
        assert owner.value(native.conn, stage, source) == original
        assert native.discovery.get_discovered_artists()[0]['because'] == ['Anchor', 'Anchor']
        regular.migrate(native.conn)
        assert not native.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        native.conn.close()
