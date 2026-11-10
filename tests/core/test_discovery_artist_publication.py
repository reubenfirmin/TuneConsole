"""Artist file selection must retain originals and leave old writers usable."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import threading

import pytest

from tests.core.test_discovery_owner_preparation import seed
from tests.store_fixture_support import (
    _numbered_input,
    mock_preparation_schema,
    mock_regular_numbered_input,
    native_sql_calls,
    numbered_store,
    numbered_store as installed,
    preparation_ddl_connection,
    publication_consumer_reads,
    reuse_reporting_reader,
    without_query_timing,
)
from yt_playlist.core import schema_inspection, collection_recovery, policy_documents
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import discovery_artists as preparation, discovery_artist_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication owners do not construct an HTTP application."""


@pytest.fixture
def unadmitted_installed(request, tmp_path, monkeypatch, native_sql_calls):
    # The consumer's native SQL dependency must precede input construction.
    # Explicit other.init_schema and every real snapshot remain in the case.
    # Only the independently admitted reader below takes a real snapshot.
    with _numbered_input(request.param, tmp_path, monkeypatch) as value:
        yield value


def prepare(conn, *, installed=False):
    # These are completed lower inputs for publication. Reuse the production
    # table-inspection session while real workers build them; all validators,
    # source copying and file writes still run.
    with schema_inspection.reuse_tables(conn):
        owner = publication.owner(conn)
        if not installed:
            owner.worker.install(conn)
        for _ in range(300):
            if owner.worker.step(conn, batch_size=8).status == 'prepared':
                return owner
        pytest.fail('Discovery explanation fixture did not prepare')


def ready(conn, size=8, *, installed=False):
    owner = prepare(conn, installed=installed)
    for _ in range(300):
        stage = owner.step(conn, batch_size=size)
        if stage.complete:
            return owner, stage.id
    pytest.fail('Discovery explanation fixture did not stage')


@pytest.mark.parametrize('unadmitted_installed', [79, 85, 86, 100], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads', 'native_sql_calls')
def test_selected_files_keep_installed_identity_counters_genres_and_preopened_readers(unadmitted_installed, monkeypatch):
    store, _ = unadmitted_installed
    _, aid, _, _ = seed(store)
    with store.conn:
        if publication.owner(store.conn).name == 'artists_legacy':
            store.conn.execute("UPDATE main.discovered_artists SET genre='Captured Unknown Genre'")
        else:
            store.conn.execute("UPDATE main.discovered_artists SET genre_key=NULL,unresolved_genre='Captured Unknown Genre'")
    expected = store.discovery.get_discovered_artists()
    worker = publication.owner(store.conn).worker
    worker.install(store.conn)
    mock_preparation_schema(monkeypatch, worker.definition, store.conn)
    originals = list(map(dict, store.conn.execute('SELECT * FROM main.discovered_artists ORDER BY artist')))
    history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    other = Store(store._db_path)
    with closing(other.conn), schema_inspection.reuse_tables(other.conn):
        other.conn.execute('PRAGMA synchronous=OFF')
        other.init_schema(defer_metadata=True)
        with other.read_snapshot() as snapshot:
            pinned = snapshot.store
            with schema_inspection.reuse_tables(pinned.conn):
                assert pinned.discovery.get_discovered_artists() == expected
                owner, stage = ready(store.conn, installed=True)
                owner.publish(store.conn, stage=stage)
                assert owner.selected(pinned.conn) is None
                assert pinned.discovery.get_discovered_artists() == expected
        with reuse_reporting_reader(other, monkeypatch), other.read_view() as selected, schema_inspection.reuse_tables(selected.conn):
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
        mock_regular_numbered_input(monkeypatch, other.conn)
        regular.migrate(other.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('damage', ['metadata', 'identity', 'file', 'part', 'plan', 'missing'])
def test_independent_verification_rejects_incorrect_files_metadata_and_coverage(installed, preparation_ddl_connection, damage):
    store, _ = installed
    seed(store)
    conn = preparation_ddl_connection
    owner = prepare(conn)
    header, parts = preparation.table(owner.name, 'generations'), preparation.table(owner.name, 'parts')
    with conn:
        if damage == 'metadata':
            conn.execute('UPDATE '+header+' SET offered_count=99')
        elif damage == 'identity':
            conn.execute('UPDATE '+header+" SET artist_id=NULL,artist_resolution='missing_canonical_artist'")
        elif damage == 'file':
            conn.execute('UPDATE '+header+" SET recovery_filename='/missing'")
        elif damage == 'part':
            conn.execute('UPDATE '+parts+" SET content_digest=printf('%064d',0)")
        elif damage == 'plan':
            conn.execute('UPDATE '+header+" SET plan_filename='/missing'")
        elif damage == 'missing':
            conn.execute('DELETE FROM '+owner.current)
    stage = owner.step(conn)
    assert stage.complete
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(conn, stage=stage.id)
    assert owner.selected(conn) is None
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
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
@pytest.mark.usefixtures('publication_consumer_reads')
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
@pytest.mark.usefixtures('publication_consumer_reads')
def test_missing_files_regular_admission_and_explicit_unchanged_source_repair(installed):
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
@pytest.mark.usefixtures('publication_consumer_reads')
def test_slow_verification_allows_real_writer_and_independent_ddl(installed, preparation_ddl_connection, monkeypatch):
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
            regular.migrate(preparation_ddl_connection)
        finally:
            release.set()
        task.result(timeout=15)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('numbered_store', [79], indirect=True)
def test_oversized_prepared_key_fails_before_map_writer_and_valid_original_is_retained(numbered_store):
    store, _ = numbered_store
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
@pytest.mark.usefixtures('publication_consumer_reads')
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
        native.conn.execute('PRAGMA synchronous=OFF')
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
