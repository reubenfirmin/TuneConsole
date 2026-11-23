"""Binary vectors keep exact bytes, coherent bases and discovery tie ordering."""
from tests.file_custody_support import export_owner, restore_owner
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, file_consumer_store
import shutil
import sqlite3
import struct

import pytest

from yt_playlist.core import embeddings, embedding_files as owner, fao
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.core.rec_model_policy import VectorChanges


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Embedding storage tests do not construct the web application."""


@pytest.fixture(scope='module')
def embedding_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def store(embedding_schema):
    database = Store(':memory:')
    try:
        initialize_consumer(database, embedding_schema)
        yield database
    finally:
        database.conn.close()


def test_basis_validation_work_grows_linearly_and_rejects_mixed_bases(store):
    # Count SQLite work, not machine-dependent elapsed time. Doubling an
    # admitted space must not quadruple the cost of validating its next rows.
    conn = store.conn
    with atomic_write(store):
        conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)',
                         [(f'basis-scale-{i}',) for i in range(1024)])
    subjects = [row[0] for row in conn.execute('SELECT id FROM recordings ORDER BY id')]
    counts = []
    for count in (512, 1024):
        steps = [0]
        def tick():
            steps[0] += 1
            return 0
        with atomic_write(store):
            conn.execute("DELETE FROM recording_embedding_files WHERE space='content_library'")
            conn.set_progress_handler(tick, 1000)
            try:
                conn.executemany('INSERT INTO recording_embedding_files('
                    'recording_id,space,model_version,dimensions,publication,position,insertion_order,built_at) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    [(rid, 'content_library', 'basis', 2, b'x' * 32, 0, i + 1, 0)
                     for i, rid in enumerate(subjects[:count])])
            finally:
                conn.set_progress_handler(None, 0)
        counts.append(steps[0])
    assert counts[1] < 2.5 * counts[0], counts
    for version, dimensions in (('other', 2), ('basis', 3)):
        with pytest.raises(sqlite3.IntegrityError, match='Mixed bases'), atomic_write(store):
            conn.execute('INSERT INTO recording_embedding_files('
                'recording_id,space,model_version,dimensions,publication,position,insertion_order,built_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (subjects[0], 'content_library', version, dimensions, b'y' * 32, 0, 1, 0))
    with pytest.raises(sqlite3.IntegrityError, match='Mixed bases'), atomic_write(store):
        conn.execute("UPDATE recording_embedding_files SET dimensions=3 WHERE space='content_library'")
    assert conn.execute("SELECT COUNT(*) FROM recording_embedding_files WHERE space='content_library'").fetchone()[0] == 1024


def test_empty_publications_clear_and_retract_each_space(store):
    for key in ('first', 'second', 'third'):
        store.tracks.upsert_track(key, key, key + ' artist', '', 100)
    recordings = [row[0] for row in store.conn.execute('SELECT identity_key FROM recordings ORDER BY id')]
    artists = [f'@artist:{row[0]}' for row in store.conn.execute('SELECT id FROM artists ORDER BY id')]
    vector = struct.pack('=ff', .25, -.0)
    expected = {}
    for name, (kind, _) in embeddings.SPACES.items():
        expected[name] = [(key, vector) for key in (recordings if kind == 'recording' else artists)]
        store.embeddings.replace(name, expected[name], {})
    for name in expected:
        before = {other: embeddings.rows(store.conn, other) for other in expected if other != name}
        store.rec_model.publish_training_generation({name: VectorChanges((), ())})
        assert dict(embeddings.rows(store.conn, name)) == dict(expected[name])
        for key, _ in expected[name]:
            store.rec_model.publish_training_generation({name: VectorChanges((), (key,))})
        assert embeddings.rows(store.conn, name) == []
        assert embeddings.head(store.conn, name) is None
        assert {other: embeddings.rows(store.conn, other) for other in before} == before
        store.embeddings.replace(name, expected[name], {})
        getattr(store.rec_model, 'replace_' + name)([])
        assert embeddings.count(store.conn, name) == 0
        assert {other: embeddings.rows(store.conn, other) for other in before} == before


def test_incremental_publication_preserves_discovery_tie_order(store):
    for key in ('zulu', 'alpha', 'middle'):
        store.tracks.upsert_track(key, key, 'Artist', '', 100)
    keys = [row[0] for row in store.conn.execute('SELECT identity_key FROM recordings ORDER BY id')]
    values = [struct.pack('=fff', n, -.0, 1e-30) for n in range(3)]
    expected = list(zip(keys, values, strict=True))
    name = 'rec_discovered_content_vectors'
    with atomic_write(store):
        embeddings.publish(store.conn, name, expected, model_version='basis')
        embeddings.publish(store.conn, name, [expected[1]], model_version='basis')
    assert embeddings.rows(store.conn, name) == expected
    assert embeddings.rows(store.conn, name, keys=keys[1:2]) == expected[1:2]
    with pytest.raises(sqlite3.IntegrityError, match='Mixed bases'), atomic_write(store):
        embeddings.publish(store.conn, name, [expected[0]], model_version='wrong')
    assert embeddings.rows(store.conn, name) == expected
    with atomic_write(store):
        embeddings.publish(store.conn, name, [expected[0]], removed=(keys[0], 'absent'), model_version='basis')
    assert embeddings.rows(store.conn, name) == expected[1:] + expected[:1]


def test_required_chunks_snapshot_and_independent_restore(store, tmp_path):
    store.tracks.upsert_track('one', 'One', 'Artist', '', 100)
    key = store.conn.execute('SELECT identity_key FROM recordings').fetchone()[0]
    vector = struct.pack('=ff', .1234567, -.0)
    with atomic_write(store):
        embeddings.publish(store.conn, 'rec_vectors', [(key, vector)], model_version='first')
    with store.read_view() as snapshot:
        store.embeddings.replace('rec_vectors', [(key, vector[::-1])], {})
        selected = next(owner.references(store.conn)).name
        assert len(selected.removesuffix('.bin')) == 64
        assert embeddings.rows(snapshot.conn, 'rec_vectors') == [(key, vector)]
        assert store.retention.collect_embeddings(10**10) == 0
    store.retention.collect_embeddings(10**10)
    manifest = export_owner(store.conn, owner, tmp_path / 'saved')
    restored = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        restore_owner(restored.conn, 'embeddings', owner, manifest)
        assert next(owner.references(restored.conn)).name == selected
        assert embeddings.rows(restored.conn, 'rec_vectors') == [(key, vector[::-1])]
        fao.write(next(owner.references(restored.conn)), b'broken')
        with pytest.raises(fao.CorruptFile):
            embeddings.rows(restored.conn, 'rec_vectors')
    finally:
        restored.conn.close()


@pytest.mark.parametrize('boundary', ['file', 'sql'])
def test_interrupted_publication_keeps_selected_vectors(store, monkeypatch, boundary):
    store.tracks.upsert_track('one', 'One', 'Artist', '', 100)
    key = store.conn.execute('SELECT identity_key FROM recordings').fetchone()[0]
    previous, replacement = struct.pack('=ff', .5, -.0), struct.pack('=ff', .75, -.0)
    store.embeddings.replace('rec_vectors', [(key, previous)], {})
    first_path = next(owner.references(store.conn))
    first_bytes = fao.read(first_path).bytes

    def interrupted_file(*args, **kwargs):
        raise OSError('interrupted')

    publish = embeddings.publish_prepared
    def interrupted_sql(*args):
        publish(*args)
        raise OSError('interrupted')

    with monkeypatch.context() as patch:
        if boundary == 'file':
            patch.setattr(fao, 'write', interrupted_file)
        else:
            patch.setattr(embeddings, 'publish_prepared', interrupted_sql)
        with pytest.raises(OSError, match='interrupted'):
            store.embeddings.replace('rec_vectors', [(key, replacement)], {})
    assert not store.conn.in_transaction
    assert store.embeddings.rows('rec_vectors') == [(key, previous)]
    assert set(owner.references(store.conn)) == {first_path}
    assert fao.read(first_path).bytes == first_bytes
    store.embeddings.replace('rec_vectors', [(key, replacement)], {})
    assert store.embeddings.rows('rec_vectors') == [(key, replacement)]


def test_training_snapshot_publishes_after_its_reader_closes(file_consumer_store):
    store = file_consumer_store
    store.tracks.upsert_track('training', 'Training', 'Artist', '', 100)
    key = store.conn.execute('SELECT identity_key FROM recordings').fetchone()[0]
    vector = struct.pack('=ff', .125, -.0)
    store.settings.set_setting('rec_dim', '2')
    with store.read_snapshot() as snapshot:
        changes = snapshot.store.rec_model.prepare_training_changes({'rec_vectors': [(key, vector)]})
        assert store.embeddings.rows('rec_vectors') == []
        assert snapshot.publish_ordered(lambda: store.rec_model.publish_training_generation(changes))
    assert store.embeddings.rows('rec_vectors') == [(key, vector)]
