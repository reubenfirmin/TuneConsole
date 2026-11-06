"""Binary vectors keep exact bytes, coherent bases and discovery tie ordering."""
from tests.file_custody_support import export_owner, restore_owner
import shutil
import sqlite3
import struct

import pytest

from yt_playlist.core import embeddings, embedding_files as owner, fao
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import embeddings as preparation
from yt_playlist.core.store import Store
from yt_playlist.repos.rec_model import VectorChanges


def test_basis_validation_work_grows_linearly_and_rejects_both_storage_forms(store):
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
                conn.executemany('INSERT INTO recording_embedding_files VALUES (?,?,?,?,?,?,?,?)',
                    [(rid, 'content_library', 'basis', 2, b'x' * 32, 0, i + 1, 0)
                     for i, rid in enumerate(subjects[:count])])
            finally:
                conn.set_progress_handler(None, 0)
        counts.append(steps[0])
    assert counts[1] < 2.5 * counts[0], counts
    with atomic_write(store):
        rid = conn.execute("INSERT INTO recordings(identity_key) VALUES ('basis-scale-installed') RETURNING id").fetchone()[0]
        conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',
                     (rid, 'content_library', 'basis', 2, b'\0' * 8, 0))
    for table in ('recording_embedding_files', 'recording_embeddings'):
        for version, dimensions in (('other', 2), ('basis', 3)):
            with pytest.raises(sqlite3.IntegrityError, match='Mixed bases'), atomic_write(store):
                if table.endswith('_files'):
                    conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)',
                        (subjects[0], 'content_library', version, dimensions, b'y' * 32, 0, 1, 0))
                else:
                    conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?,?)',
                        (subjects[0], 'content_library', version, dimensions, b'\0' * (dimensions * 4), 0))
        with pytest.raises(sqlite3.IntegrityError, match='Mixed bases'), atomic_write(store):
            conn.execute(f"UPDATE {table} SET dimensions=3 WHERE space='content_library'")
    assert conn.execute("SELECT COUNT(*) FROM recording_embedding_files WHERE space='content_library'").fetchone()[0] == 1024


def test_empty_publications_clear_and_retract_each_space_during_partial_conversion(store):
    for key in ('first', 'second', 'third'):
        store.tracks.upsert_track(key, key, key + ' artist', '', 100)
    recordings = list(store.conn.execute('SELECT id,identity_key FROM recordings ORDER BY id'))
    artists = [row[0] for row in store.conn.execute('SELECT id FROM artists ORDER BY id')]
    vector = struct.pack('=ff', .25, -.0)
    names = ('rec_vectors', 'rec_content_vectors', 'rec_discovered_content_vectors',
             'rec_artist_vectors', 'rec_artist_content_vectors')
    expected = {}
    for name in names:
        kind, space, table, column = embeddings.descriptor(name)
        subjects = recordings if kind == 'recording' else [(aid, '@artist:' + str(aid)) for aid in artists]
        expected[name] = [(key, vector) for _, key in subjects]
        with atomic_write(store):
            store.conn.executemany(f'INSERT INTO {table} VALUES (?,?,?,?,?,?)',
                [(sid, space, 'basis', 2, vector, 1) for sid, _ in subjects])
            # Keep two installed owners beside one converted owner in every space.
            owner.publish(store.conn, kind, space, 'basis', 2,
                          [(subjects[0][0], vector, 1, 1)])
        assert len(embeddings.rows(store.conn, name)) == 3
    for name in names:
        before = {other: embeddings.rows(store.conn, other) for other in names if other != name}
        with atomic_write(store):
            store.rec_model.publish_training_generation({name: VectorChanges((), ())})
        assert embeddings.rows(store.conn, name) == expected[name]
        # Remove a converted owner and an installed owner, then the final owner.
        for key, _ in expected[name]:
            with atomic_write(store):
                store.rec_model.publish_training_generation({name: VectorChanges((), (key,))})
        assert embeddings.rows(store.conn, name) == []
        assert embeddings.head(store.conn, name) is None
        assert {other: embeddings.rows(store.conn, other) for other in before} == before
        # Exercise the public wholesale clear too, with both forms present again.
        kind, space, table, column = embeddings.descriptor(name)
        subjects = recordings if kind == 'recording' else [(aid, '@artist:' + str(aid)) for aid in artists]
        with atomic_write(store):
            store.conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?,?)',
                               (subjects[1][0], space, 'basis', 2, vector, 1))
            owner.publish(store.conn, kind, space, 'basis', 2, [(subjects[0][0], vector, 1, 1)])
        getattr(store.rec_model, 'replace_' + name)([])
        assert embeddings.count(store.conn, name) == 0
        assert not store.conn.execute(f'SELECT 1 FROM {table} WHERE space=?', (space,)).fetchone()
        assert not store.conn.execute(f'SELECT 1 FROM {kind}_embedding_files WHERE space=?', (space,)).fetchone()
        assert {other: embeddings.rows(store.conn, other) for other in before} == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_partial_conversion_and_incremental_publication_preserve_order(store, monkeypatch):
    for key in ('zulu', 'alpha', 'middle'):
        store.tracks.upsert_track(key, key, 'Artist', '', 100)
    subjects = list(store.conn.execute('SELECT id,identity_key FROM recordings ORDER BY id'))
    values = [struct.pack('=fff', n, -.0, 1e-30) for n in range(3)]
    with atomic_write(store):
        for (subject, key), vector in zip(subjects, values):
            store.conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',
                (subject, 'content_discovery', 'basis', 3, vector, 1))
    expected = [(key, vector) for (_, key), vector in zip(subjects, values)]
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors') == expected
    with monkeypatch.context() as patch:
        patch.setattr(fao, 'write', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError, match='disk full'):
            preparation.prepare(store, limit=1)
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors') == expected
    assert preparation.prepare(store, limit=1) == 1
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors') == expected
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors', keys=[subjects[1][1]]) == expected[1:2]
    with atomic_write(store):
        embeddings.publish(store.conn, 'rec_discovered_content_vectors', [expected[1]], model_version='basis')
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors') == expected
    while preparation.prepare(store, limit=1):
        pass
    assert embeddings.count(store.conn, 'rec_discovered_content_vectors') == 3
    assert not store.conn.execute('SELECT 1 FROM recording_embeddings').fetchone()
    with pytest.raises(Exception, match='Mixed bases'), atomic_write(store):
        embeddings.publish(store.conn, 'rec_discovered_content_vectors', [expected[0]], model_version='wrong')
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors') == expected
    # A retraction followed by the same subject's upsert appends it; an absent
    # raw identity has no effect on the maximum or the retained subjects.
    with atomic_write(store):
        embeddings.publish(store.conn, 'rec_discovered_content_vectors', [expected[0]],
                           removed=(expected[0][0], 'absent-owner'), model_version='basis')
    assert embeddings.rows(store.conn, 'rec_discovered_content_vectors') == expected[1:] + expected[:1]


def test_required_chunks_snapshot_and_independent_restore(store, tmp_path):
    store.tracks.upsert_track('one', 'One', 'Artist', '', 100)
    key = store.conn.execute('SELECT identity_key FROM recordings').fetchone()[0]
    vector = struct.pack('=ff', .1234567, -.0)
    with atomic_write(store):
        embeddings.publish(store.conn, 'rec_vectors', [(key, vector)], model_version='first')
    with store.read_view() as snapshot:
        with atomic_write(store):
            embeddings.publish(store.conn, 'rec_vectors', [(key, vector[::-1])], replace=True, model_version='second')
        assert embeddings.rows(snapshot.conn, 'rec_vectors') == [(key, vector)]
        assert store.retention.collect_embeddings(10**10) == 0
    store.retention.collect_embeddings(10**10)
    manifest = export_owner(store.conn, owner, tmp_path / 'saved')
    restored = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        restore_owner(restored.conn, 'embeddings', owner, manifest)
        assert embeddings.rows(restored.conn, 'rec_vectors') == [(key, vector[::-1])]
        fao.write(next(owner.references(restored.conn)), b'broken')
        with pytest.raises(fao.CorruptFile):
            embeddings.rows(restored.conn, 'rec_vectors')
    finally:
        restored.conn.close()


@pytest.mark.parametrize('converted',[1,3])
def test_installed_append_after_conversion_and_restart_preserves_discovery_order(tmp_path,converted):
    database = tmp_path/'installed.db'
    store = Store(database)
    try:
        store.init_schema()
        for key in ('zulu','alpha','middle','newest'):
            store.tracks.upsert_track(key,key,'Artist','',100)
        subjects = list(store.conn.execute('SELECT id,identity_key FROM recordings ORDER BY id'))
        vector = struct.pack('=ff',.5,-.0)
        with atomic_write(store):
            store.conn.executemany('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',[
                (rid,'content_discovery','basis',2,vector,1) for rid,_ in subjects[:3]])
        assert preparation.prepare(store,limit=converted)==converted
    finally:
        store.conn.close()
    store = Store(database)
    try:
        store.init_schema()
        with atomic_write(store):
            store.conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',
                (subjects[3][0],'content_discovery','basis',2,vector,2))
        expected = [(key,vector) for _,key in subjects]
        assert embeddings.rows(store.conn,'rec_discovered_content_vectors')==expected
        # Upserting a converted original keeps its original tied-rank position.
        replacement = struct.pack('=ff',.75,-.0)
        with atomic_write(store):
            store.conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?) '
                'ON CONFLICT(recording_id,space,model_version) DO UPDATE SET vector=excluded.vector',
                (subjects[0][0],'content_discovery','basis',2,replacement,3))
        expected[0] = (subjects[0][1],replacement)
        assert embeddings.rows(store.conn,'rec_discovered_content_vectors')==expected
        while preparation.prepare(store,limit=1):
            pass
        assert embeddings.rows(store.conn,'rec_discovered_content_vectors')==expected
        assert not store.conn.execute('SELECT 1 FROM recording_embedding_legacy_order').fetchone()
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()
