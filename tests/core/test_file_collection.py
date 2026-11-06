"""Count real traversal work across dense shards, restarts and owner fences."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import (fao, file_collection, file_map, legacy_journals,
                              policy_documents, policy_history, vibe_provenance)
from yt_playlist.core.evidence_ledger import canonical


@pytest.mark.parametrize('domain', ['policy', 'provenance', 'archive'])
def test_collection_bounds_all_entries_and_resumes_with_current_owners(tmp_path, monkeypatch, domain):
    database, root = tmp_path / 'state.db', tmp_path / 'files'
    conn = sqlite3.connect(database, timeout=0)
    owner = {'policy': policy_history, 'provenance': vibe_provenance, 'archive': legacy_journals}[domain]
    data_root = root / 'album' if domain == 'archive' else root
    monkeypatch.setattr(owner, 'directory', lambda _conn: root)
    paths, payloads = [], []
    if domain != 'provenance':
        if domain == 'archive':
            conn.execute(owner.SQL)
        else:
            conn.execute('CREATE TABLE policy_history_archive(id INTEGER PRIMARY KEY,publication BLOB)')
        number = 0
        while len(paths) < 130:
            raw = canonical({'format': file_map.FORMAT, 'entries': {str(number): {'original': number}}})
            number += 1
            digest = hashlib.sha256(raw).hexdigest()
            if not digest.startswith('00'):
                continue
            paths.append(data_root / file_map.relative(digest))
            payloads.append(raw)
        if domain == 'archive':
            conn.execute("INSERT INTO legacy_archive_heads VALUES ('album',?)", (bytes.fromhex(paths[0].stem),))
        else:
            conn.execute('INSERT INTO policy_history_archive VALUES (1,?)', (bytes.fromhex(paths[0].stem),))
        shard, other_shard, checkpoint = '00', 'ff', '.collect.json'
    else:
        conn.execute('CREATE TABLE prediction_provenance(prediction_id INTEGER PRIMARY KEY,state TEXT)')
        conn.execute("INSERT INTO prediction_provenance VALUES (1,'ready')")
        for group in range(130):
            pid = group * owner.CHUNK_ROWS + 1
            paths.append(root / owner.relative(pid))
            payloads.append(canonical({'format': 1, 'calculations': {
                str(pid): {'inputs': [17, 23], 'artists': [3]}}}))
        shard, other_shard, checkpoint = '0', '1', '.collection.json'
    conn.commit()
    for path, raw in zip(paths, payloads):
        fao.write(path, raw)
    if domain != 'provenance':
        # Keep a reachable child as well as the SQL root itself.
        key = next(iter(json.loads(payloads[0])['entries']))
        publication = file_map._write(data_root, {'children': {file_map._key(key)[0]: paths[0].stem}})
        table = 'legacy_archive_heads' if domain == 'archive' else 'policy_history_archive'
        conn.execute('UPDATE ' + table + ' SET publication=?', (bytes.fromhex(publication),))
        conn.commit()
        current_root = data_root / file_map.relative(publication)
        original_root = fao.read(current_root).bytes
    extra_orphans = []
    if domain == 'archive':
        for namespace in ('catalog', 'workflow'):
            digest = file_map.update(root / namespace, None, {'unpublished': {'original': namespace}})
            extra_orphans.append(root / namespace / file_map.relative(digest))
    # Hidden entries, unsupported files, nested directories and shard discovery
    # all consume traversal work, even though none can be collected as a body.
    for base in (data_root, data_root / shard):
        for i in range(24):
            fao.write(base / ('.hidden-' + str(i)), b'hidden')
        fao.write(base / 'unsupported.txt', b'keep')
    nested = data_root / shard / 'nested'
    fao.write(nested / 'untouched.json', b'nested')
    fao.write(data_root / other_shard / '.keep', b'keep')
    examined, opened = [], []
    walk = file_collection.walk_entries

    def counted(*args, **kwargs):
        opened.append(args[0])
        for path in walk(*args, **kwargs):
            examined.append((path, conn.in_transaction))
            yield path

    monkeypatch.setattr(file_collection, 'walk_entries', counted)
    monkeypatch.setattr(Path, 'glob', lambda *a, **k: pytest.fail('Unbounded owner glob'))

    def step(now):
        examined.clear()
        opened.clear()
        result = owner.collect(conn, now, limit=16)
        assert len(examined) <= 16 and len(opened) <= 16
        assert all(reserved for _, reserved in examined)
        return result

    try:
        for _ in range(16):
            step(time.time())
        assert all(path.exists() for path in paths)  # Thirty-day orphan grace.
        with policy_documents.retained_reader(root):
            assert step(10**10) == 0
            assert not examined and not opened
        other = sqlite3.connect(database)
        try:
            other.execute('BEGIN IMMEDIATE')
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                step(10**10)
            assert all(path.exists() for path in paths)
        finally:
            other.rollback()
            other.close()
        for _ in range(12):
            step(10**10)
            if sum(path.exists() for path in paths) < 130:
                break
        assert 1 < sum(path.exists() for path in paths) < 130
        # Close the cached directory handles as an application shutdown does.
        for key in list(file_collection._CURSORS):
            if key[0] is conn:
                file_collection._CURSORS.pop(key).close()
        conn.close()
        conn = sqlite3.connect(database, timeout=0)
        for _ in range(100):
            step(10**10)
            if not any(path.exists() for path in paths[1:] + extra_orphans):
                break
        assert all(not path.exists() for path in paths[1:])
        assert all(not path.exists() for path in extra_orphans)
        assert fao.read(paths[0]).bytes == payloads[0]
        if domain != 'provenance':
            assert fao.read(current_root).bytes == original_root
        assert fao.read(nested / 'untouched.json').bytes == b'nested'
        saved = json.loads(fao.read(root / checkpoint).bytes)
        assert set(saved) == ({'shard', 'after_shard', 'prefix'} if domain == 'archive'
                              else {'shard', 'after_shard'})
        # A superseded current body becomes collectible on a later wrap.
        if domain != 'provenance':
            table = 'legacy_archive_heads' if domain == 'archive' else 'policy_history_archive'
            conn.execute('DELETE FROM ' + table)
        else:
            conn.execute("UPDATE prediction_provenance SET state='expired'")
        conn.commit()
        for _ in range(100):
            step(10**10)
            if not paths[0].exists():
                break
        assert not paths[0].exists()
    finally:
        for key in list(file_collection._CURSORS):
            if key[1] == root:
                file_collection._CURSORS.pop(key).close()
        conn.close()


def test_provenance_collection_validates_every_calculation_before_repacking(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / 'state.db')
    root = tmp_path / 'files'
    monkeypatch.setattr(vibe_provenance, 'directory', lambda _conn: root)
    conn.execute('CREATE TABLE prediction_provenance(prediction_id INTEGER PRIMARY KEY,state TEXT)')
    conn.execute("INSERT INTO prediction_provenance VALUES (1,'ready')")
    conn.commit()
    path = root / vibe_provenance.relative(1)
    fao.write(path, canonical({'format': 1, 'calculations': {'1': {}, '129': {}}}))
    original = path.read_bytes()
    try:
        with pytest.raises(fao.CorruptFile, match='chunk owner'):
            vibe_provenance.collect(conn, 10**10)
        assert path.read_bytes() == original
    finally:
        for key in list(file_collection._CURSORS):
            if key[0] is conn:
                file_collection._CURSORS.pop(key).close()
        conn.close()
