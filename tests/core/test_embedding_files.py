"""Binary vectors keep exact bytes, coherent bases and discovery tie ordering."""
import shutil
import struct

import pytest

from yt_playlist.core import embeddings, embedding_files as owner, fao
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import embeddings as preparation
from yt_playlist.core.store import Store
from yt_playlist.repos.rec_model import VectorChanges


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
        patch.setattr(fao, 'write_bytes', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
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
        assert preparation.maintain(store, 10**10) == 0
    preparation.maintain(store, 10**10)
    manifest = owner.export(store.conn, tmp_path / 'saved')
    restored = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        owner.restore(restored.conn, manifest)
        assert embeddings.rows(restored.conn, 'rec_vectors') == [(key, vector[::-1])]
        fao.write_bytes(next(owner.references(restored.conn)), b'broken')
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
