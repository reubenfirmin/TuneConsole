"""Genre publication proves exact coverage without changing installed writers."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from yt_playlist.core import albums, genre_classifications, genre_identity
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import genre_classifications as preparation, genre_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo


def seed(store):
    conn = store.conn
    with conn:
        genre_identity.seed_catalog(conn)
        conn.executemany('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
            [(index, 'genre-publication:'+str(index), label) for index, label in enumerate(
                ('Drum And Bass', None, '', 'Unregistered style', 'Ambiguous'), 1)])
        conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
            [('ambiguous-one', 'Ambiguous'), ('ambiguous-two', 'Second')])
        conn.execute("INSERT INTO genre_alias VALUES ('ambiguous','Ambiguous','ambiguous-two','custom')")
        album = albums.observe(conn, 'genre-album', title='Release', artist='Artist', genre='Synth Pop')
    store.discovery.upsert_discovered_artist('Candidate', 1, [], [], None, 10, genre='Jazz')
    return album


def ready(conn, size=2):
    preparation.JOB.install(conn)
    for _ in range(100):
        if preparation.JOB.step(conn, batch_size=size).status == 'prepared':
            break
    else:
        pytest.fail('Genre preparation did not complete')
    for _ in range(100):
        stage = publication.step(conn, batch_size=size)
        if stage.complete:
            return stage.id
    pytest.fail('Genre publication did not stage')


@pytest.mark.parametrize('installed', [79, 85], indirect=True)
def test_selection_keeps_null_blank_ambiguity_and_all_originals_with_pinned_readers(installed):
    store, _ = installed
    album = seed(store)
    conn = store.conn
    before = {owner: list(map(tuple, conn.execute('SELECT * FROM main.'+owner))) for owner in preparation.JOB.keys}
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        with other.read_snapshot() as snapshot:
            pinned = snapshot.store
            assert genre_classifications.read(pinned.conn, 'recordings', 1) == 'Drum And Bass'
            stage = ready(conn, 1)
            publication.publish(conn, identity=stage)
            assert publication.selected(pinned.conn) is None
            assert genre_classifications.read(pinned.conn, 'recordings', 1) == 'Drum And Bass'
        canonical = conn.execute("SELECT name FROM genre_catalog WHERE key='drum bass'").fetchone()[0]
        with other.read_view() as reader:
            def deny_label(action, owner, column, *_):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and owner in preparation.JOB.keys and column == 'genre' else sqlite3.SQLITE_OK
            reader.conn.set_authorizer(deny_label)
            assert [genre_classifications.read(reader.conn, 'recordings', key) for key in range(1, 6)] == [
                canonical, None, '', 'Unregistered style', 'Ambiguous']
            assert genre_classifications.read(reader.conn, 'albums', album) == conn.execute(
                "SELECT name FROM genre_catalog WHERE key='synth pop'").fetchone()[0]
            # Candidate explanations still use installed fields in this test.
            # Mask only their unused raw label, including SELECT d.*; the
            # explicitly selected genre column must supply the display value.
            reader.conn.set_authorizer(lambda action, owner, column, *_: sqlite3.SQLITE_IGNORE
                if action == sqlite3.SQLITE_READ and owner in preparation.JOB.keys and column == 'genre' else sqlite3.SQLITE_OK)
            assert reader.discovery.get_discovered_artists()[0]['genre'] == 'Jazz'
        assert publication.step(conn).id == stage
        publication.publish(conn, identity=stage)
        assert conn.execute('SELECT count(*) FROM '+publication.table('generations')).fetchone()[0] == 1
        rows = conn.execute('SELECT disposition,source_snapshot FROM '+publication.table('rows')+
            " WHERE stage=? AND owner='recordings' ORDER BY source_key", (stage,)).fetchall()
        assert [row[0] for row in rows] == ['resolved', 'null', 'blank', 'unknown_genre', 'ambiguous_genre']
        assert [json.loads(row[1])['label'] for row in rows] == [
            'Drum And Bass', None, '', 'Unregistered style', 'Ambiguous']
        for owner, original in before.items():
            assert list(map(tuple, conn.execute('SELECT * FROM main.'+owner))) == original
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_continuing_label_and_reference_writes_withdraw_and_reconcile(installed):
    store, _ = installed
    seed(store)
    conn = store.conn
    first = ready(conn)
    publication.publish(conn, identity=first)
    with conn:
        genre_classifications.write(conn, 'recordings', 1, 'New Unknown')
        conn.execute('DELETE FROM recordings WHERE id=2')
        conn.execute("INSERT INTO recordings(id,identity_key,genre) VALUES (0,'new-low-key','Rock')")
    assert publication.selected(conn) is None
    assert genre_classifications.read(conn, 'recordings', 1) == 'New Unknown'
    with pytest.raises(MigrationError, match='reconciled'):
        publication.publish(conn, identity=first)
    second = ready(conn)
    publication.publish(conn, identity=second)
    assert second != first and genre_classifications.read(conn, 'recordings', 1) == 'New Unknown'
    with conn:
        conn.execute("UPDATE genre_catalog SET name='Renamed Rock' WHERE key='rock'")
    assert publication.selected(conn) is None
    third = ready(conn)
    publication.publish(conn, identity=third)
    assert genre_classifications.read(conn, 'recordings', 0) == 'Renamed Rock'
    assert conn.execute('SELECT count(*) FROM '+publication.table('rows')+' WHERE stage=?', (first,)).fetchone()[0] == 7
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        conn.execute('DELETE FROM prepared_genre_originals')
    conn.rollback()


@pytest.mark.parametrize('installed', [79, 85], indirect=True)
def test_catalog_and_analytics_read_selected_labels_and_keep_user_override(installed):
    store, _ = installed
    seed(store)
    entry = store.tracks.upsert_track('genre-upload', 'Track', 'Artist', None, 100)
    overridden = store.tracks.upsert_track('genre-user', 'User track', 'Artist', None, 100)
    with store.conn:
        store.conn.execute("UPDATE recordings SET genre='Drum And Bass' WHERE id IN "
            '(SELECT recording_id FROM catalog_entries WHERE id IN (?,?))', (entry, overridden))
    store.tracks.set_track_genre(overridden, 'Exact user choice')
    stage = ready(store.conn)
    publication.publish(store.conn, identity=stage)
    canonical = store.conn.execute("SELECT name FROM genre_catalog WHERE key='drum bass'").fetchone()[0]
    with store.read_view() as reader:
        reader.conn.set_authorizer(lambda action, owner, column, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and owner in preparation.JOB.keys and column == 'genre' else sqlite3.SQLITE_OK)
        assert reader.tracks.track_detail('genre-upload')['genre'] == canonical
        assert reader.tracks.track_detail('genre-user')['genre'] == 'Exact user choice'
        rows = {row['video_id']: row for batch in ListeningAnalyticsRepo(reader).catalog_rows() for row in batch}
        assert rows['genre-upload']['genre'] == canonical
    assert store.tracks.track_detail('genre-upload')['genre'] == canonical
    assert store.tracks.track_detail('genre-user')['genre'] == 'Exact user choice'


@pytest.mark.parametrize('installed', [79, 85], indirect=True)
def test_genre_and_canonical_discovery_selections_compose(installed):
    from tests.core.test_discovery_owner_preparation import seed as seed_discovery
    from tests.core.test_discovery_publication import ready as discovery_ready
    from yt_playlist.core.preparation import discovery_publication
    store, _ = installed
    _, aid, rid, album = seed_discovery(store)
    with store.conn:
        genre_identity.seed_catalog(store.conn)
        store.conn.execute("UPDATE recordings SET genre='Drum And Bass' WHERE id=?", (rid,))
        store.conn.execute("UPDATE albums SET genre='Synth Pop' WHERE id=?", (album,))
        store.conn.execute("UPDATE discovered_artists SET genre='Drum And Bass' WHERE artist_id=?", (aid,))
    stage = ready(store.conn)
    publication.publish(store.conn, identity=stage)
    owner, discovery_stage = discovery_ready(store.conn)
    owner.publish(store.conn, stage=discovery_stage)
    canonical = dict(store.conn.execute("SELECT key,name FROM genre_catalog WHERE key IN ('drum bass','synth pop')"))
    assert publication.selected(store.conn) == stage
    with store.read_view() as reader:
        assert discovery_publication.read_owners(reader.conn).revision == discovery_stage
        reader.conn.set_authorizer(lambda action, source, column, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and source in preparation.JOB.keys and column == 'genre' else sqlite3.SQLITE_OK)
        assert reader.discovery.get_discovered_tracks()[0]['genre'] == canonical['drum bass']
        assert reader.discovery.get_discovered_albums()[0]['genre'] == canonical['synth pop']
        assert reader.discovery.get_discovered_artists()[0]['genre'] == canonical['drum bass']
        assert reader.discovery.present_home_tiles(['release'], [aid])[0]['release']['genre'] == canonical['synth pop']


@pytest.mark.parametrize('installed', [85], indirect=True)
@pytest.mark.parametrize('damage', ['typed', 'raw', 'missing'])
def test_independent_verification_rejects_preparation_damage(installed, damage):
    store, _ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    while preparation.JOB.step(conn, batch_size=2).status != 'prepared':
        pass
    with conn:
        if damage == 'typed':
            conn.execute("UPDATE prepared_genre_classifications SET genre_key='jazz' WHERE owner='recordings' AND source_key='[1]'")
        elif damage == 'raw':
            conn.execute("UPDATE prepared_genre_originals SET source_snapshot='{}' WHERE owner='recordings' AND source_key='[1]'")
        else:
            conn.execute("DELETE FROM prepared_genre_classifications WHERE owner='recordings' AND source_key='[1]'")
    stage = publication.step(conn, batch_size=16)
    assert stage.complete
    with pytest.raises(MigrationError, match='differs|missing|extra'):
        publication.publish(conn, identity=stage.id)
    assert publication.selected(conn) is None


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_interrupted_batch_resume_and_final_fence_rejects_late_writer(installed, monkeypatch):
    store, _ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    while preparation.JOB.step(conn, batch_size=2).status != 'prepared':
        pass
    prefix = publication.step(conn, batch_size=1)
    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('UPDATE '+publication.table('stages')):
                raise KeyboardInterrupt()
            return super().execute(sql, parameters)
    with sqlite3.connect(store._db_path, factory=InterruptedConnection) as interrupted:
        interrupted.row_factory = sqlite3.Row
        interrupted.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(KeyboardInterrupt):
            publication.step(interrupted, batch_size=1)
    assert publication.stage(conn, prefix.id) == prefix
    assert conn.execute('SELECT count(*) FROM '+publication.table('rows')+' WHERE stage=?', (prefix.id,)).fetchone()[0] == 1
    stage = ready(conn)
    assert stage == prefix.id
    verify = publication.verify
    def write_after_verify(connection, identity):
        result = verify(connection, identity)
        with connection:
            connection.execute("UPDATE recordings SET genre='Late edit' WHERE id=1")
        return result
    monkeypatch.setattr(publication, 'verify', write_after_verify)
    with pytest.raises(MigrationError, match='reconciled|changed'):
        publication.publish(conn, identity=stage)
    assert publication.selected(conn) is None


@pytest.mark.parametrize('installed', [85], indirect=True)
def test_slow_verification_keeps_writes_and_regular_admission_available(installed, monkeypatch, independent_regular_ddl):
    store, _ = installed
    seed(store)
    stage = ready(store.conn)
    entered, release = threading.Event(), threading.Event()
    describe, worker_thread = preparation.describe, None
    def slow(*args):
        if threading.get_ident() == worker_thread:
            entered.set()
            assert release.wait(15)
        return describe(*args)
    monkeypatch.setattr(preparation, 'describe', slow)
    def publish():
        nonlocal worker_thread
        worker_thread = threading.get_ident()
        with sqlite3.connect(store._db_path, timeout=.1) as conn:
            conn.row_factory = sqlite3.Row
            with pytest.raises(MigrationError, match='reconciled|changed'):
                publication.publish(conn, identity=stage)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(publish)
        try:
            assert entered.wait(15)
            with store.conn:
                genre_classifications.write(store.conn, 'recordings', 1, 'Concurrent edit')
            regular.migrate(store.conn)
        finally:
            release.set()
        task.result(timeout=15)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_oversized_source_fails_durably_and_regular_schema_remains_independent(installed, independent_regular_ddl):
    store, _ = installed
    seed(store)
    conn = store.conn
    with conn:
        conn.execute('UPDATE recordings SET genre=? WHERE id=1', ('x'*(preparation.MAX_LABEL_BYTES+1),))
    preparation.JOB.install(conn)
    with pytest.raises(ValueError, match='byte budget'):
        preparation.JOB.step(conn)
    failed = preparation.JOB.progress(conn)
    assert failed.status == 'failed'
    regular.migrate(conn)
    assert preparation.JOB.progress(conn) == failed
    with conn:
        genre_classifications.write(conn, 'recordings', 1, 'Jazz')
    preparation.JOB.step(conn, retry=True)
    stage = ready(conn)
    publication.publish(conn, identity=stage)


@pytest.mark.parametrize('installed', [79, 85], indirect=True)
def test_native_takeover_preserves_retained_selection_inputs(installed, tmp_path):
    store, version = installed
    seed(store)
    stage = ready(store.conn)
    publication.publish(store.conn, identity=stage)
    rows = list(map(tuple, store.conn.execute('SELECT * FROM '+publication.table('rows'))))
    path = tmp_path/'native.db'
    with sqlite3.connect(path) as conn:
        store.conn.backup(conn)
        conn.row_factory = sqlite3.Row
        runner.migrate(conn, registry=registry.MIGRATIONS[:86])
        regular.migrate(conn)
        assert publication.selected(conn) is None
        assert list(map(tuple, conn.execute('SELECT * FROM '+publication.table('rows')))) == rows
