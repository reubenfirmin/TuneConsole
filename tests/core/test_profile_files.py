"""Exact profile heads survive partial conversion, failed writes and recovery."""
import hashlib
import json
import shutil
import sqlite3
import time

import pytest

from yt_playlist.core import fao, policy_documents, profile_files as owner
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import profiles
from yt_playlist.core.store import Store


def payload(value):
    raw = json.dumps({'dimensions': {}, 'evidence': value}, indent=2)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def seed(store):
    for name in ('one', 'two'):
        store.tracks.upsert_track(name, name, 'Artist', '', 100)
    keys = [r[0] for r in store.conn.execute('SELECT identity_key FROM recordings ORDER BY id')]
    bodies = {}
    with atomic_write(store):
        for key in keys:
            raw, digest = payload(key)
            store.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)', (key, raw, 'v1', digest, 1))
            bodies[key] = raw
    return keys, bodies


def test_exact_partial_conversion_budgets_and_failed_file_publication(store, monkeypatch):
    keys, bodies = seed(store)
    with monkeypatch.context() as patch:
        patch.setattr(fao, 'write_bytes', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError, match='disk full'):
            profiles.prepare(store, limit=1)
    assert dict(store.conn.execute('SELECT identity_key,payload FROM rec_musical_profiles')) == bodies
    assert profiles.prepare(store, limit=1) == 1
    assert [owner.read(store.conn, 'recording', key) for key in keys] == [bodies[key] for key in keys]
    assert profiles.prepare(store, limit=1) == 1
    assert profiles.prepare(store, limit=1) == 0
    assert len(list(owner.references(store.conn))) == 2
    assert store.musical_profiles.by_keys(keys) == {key: json.loads(bodies[key]) for key in keys}
    with pytest.raises(fao.FileBudgetExceeded):
        owner.read(store.conn, 'recording', keys[0], max_bytes=1)
    with pytest.raises(Exception, match='work budget'):
        owner.read(store.conn, 'recording', keys[0], cache=owner.Cache(work_bytes=1))


def test_snapshot_retains_old_body_and_independent_restore(store, tmp_path):
    keys, bodies = seed(store)
    profiles.prepare(store)
    with store.read_view() as snapshot:
        before = owner.read(snapshot.conn, 'recording', keys[0])
        raw, digest = payload('successor')
        with atomic_write(store):
            store.musical_profiles.publish_recordings([(keys[0], raw, 'v2', digest)], 2)
        assert owner.read(snapshot.conn, 'recording', keys[0]) == before
        assert profiles.maintain(store, 10**10) == 0
    assert owner.read(store.conn, 'recording', keys[0]) == raw
    profiles.maintain(store, 10**10)
    assert owner.read(store.conn, 'recording', keys[1]) == bodies[keys[1]]
    manifest = owner.export(store.conn, tmp_path / 'saved')
    restored = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        owner.restore(restored.conn, manifest)
        restored.init_schema()
        assert restored.musical_profiles.by_keys(keys)[keys[0]] == json.loads(raw)
        path = next(owner.references(restored.conn))
        fao.write_bytes(path, b'{}')
        with pytest.raises(fao.CorruptFile):
            restored.musical_profiles.by_keys(keys)
    finally:
        restored.conn.close()


@pytest.mark.parametrize('extension', ('json', 'bin'))
def test_collection_bounds_dense_shards_and_resumes_behind_reader_and_writer_fences(tmp_path, monkeypatch, extension):
    root, database = tmp_path/'publications', tmp_path/'state.db'
    conn = sqlite3.connect(database, timeout=0)
    previous = None
    try:
        conn.execute('CREATE TABLE heads(publication BLOB PRIMARY KEY)')
        # Genuine content hashes concentrated in one shard challenge traversal
        # continuation, rather than assuming every shard fits in one turn.
        paths, publications = [], []
        number = 0
        while len(paths) < 130:
            encoded = json.dumps({'publication': number}).encode()
            number += 1
            publication = hashlib.sha256(encoded).digest()
            if publication[0] != 0:
                continue
            path = root/'recording'/'00'/(publication.hex()+'.'+extension)
            fao.write_bytes(path, encoded, immutable=True)
            paths.append(path)
            publications.append(publication)
        conn.execute('INSERT INTO heads VALUES (?)', (publications[0],))
        conn.commit()
        examined, opened = [], []
        walk = fao.walk_entries

        def counted(*args, **kwargs):
            opened.append(args[0])
            for entry in walk(*args, **kwargs):
                examined.append((entry, conn.in_transaction))
                yield entry

        monkeypatch.setattr(fao, 'walk_entries', counted)
        monkeypatch.setattr(fao, 'listing', lambda *a, **k: pytest.fail('Collection must not enumerate the owner'))

        def step(now):
            examined.clear()
            opened.clear()
            removed = owner.collect_unreferenced(conn, root, {'recording': 'heads'}, now, 16, '*/*/*.'+extension)
            assert len(examined) <= 16 and len(opened) <= 16
            assert all(reserved for _, reserved in examined)
            return removed

        assert step(time.time()) == 0  # Recent orphans retain their age policy.
        with policy_documents.retained_reader(root):
            assert step(10**10) == 0
            assert not examined and not opened
        # A file whose publisher has not committed must not become collectible.
        other = sqlite3.connect(database)
        try:
            other.execute('BEGIN IMMEDIATE')
            other.execute('INSERT INTO heads VALUES (?)', (publications[1],))
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                step(10**10)
            assert paths[1].exists()
            other.commit()
        finally:
            other.close()
        removed = step(10**10)
        assert 0 < removed < 128
        previous = conn
        previous.close()
        conn = sqlite3.connect(database, timeout=0)
        # Restart re-examines at most the partially completed hash shard.
        for _ in range(100):
            removed += step(10**10)
            if removed == 128:
                break
        assert removed == 128
        assert all(not path.exists() for path in paths[2:])
        assert all(path.exists() for path in paths[:2])
        assert step(10**10) == 0
        assert 0 < json.loads(fao.read_bytes(root/'.collection.json'))['shard'] < 256
        conn.execute('DELETE FROM heads WHERE publication=?', (publications[0],))
        conn.commit()
        previous = conn
        previous.close()
        conn = sqlite3.connect(database, timeout=0)
        # A completed-shard bookmark survives restart. The newly superseded
        # first-shard publication is visited only when the round-robin wraps.
        assert step(10**10) == 0
        assert paths[0].exists()
        for _ in range(100):
            step(10**10)
            if not paths[0].exists():
                break
        assert not paths[0].exists() and paths[1].exists()
    finally:
        conn.close()
        if previous is not None:
            previous.close()
