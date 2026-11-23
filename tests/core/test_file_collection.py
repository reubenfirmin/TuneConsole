"""Count real traversal work across dense shards, restarts and owner fences."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import fao, file_collection, file_map, legacy_journals, policy_documents, policy_history_read
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.policy_files import PolicyFiles


@pytest.mark.parametrize('domain', ['policy', 'archive'])
def test_collection_bounds_all_entries_and_resumes_with_current_owners(
        tmp_path, monkeypatch, domain, _pristine_current_schema):
    database, root = tmp_path / 'state.db', tmp_path / 'files'
    conn = sqlite3.connect(database, timeout=0)
    data_root = root / 'album' if domain == 'archive' else root
    files = PolicyFiles(tmp_path / 'current', root)
    monkeypatch.setattr(legacy_journals, 'directory', lambda _conn: root)
    paths, payloads = [], []
    if domain == 'archive':
        conn.execute(legacy_journals.SQL)
    else:
        _pristine_current_schema.backup(conn)
    number = 0
    while len(paths) < 130:
        value = {'original': number} if domain == 'archive' else {
            'state': 'archived', 'configuration': {'original': number}, 'base': None,
        }
        raw = canonical({'format': file_map.FORMAT, 'entries': {str(number): value}})
        number += 1
        digest = hashlib.sha256(raw).hexdigest()
        if not digest.startswith('00'):
            continue
        paths.append(data_root / file_map.relative(digest))
        payloads.append(raw)
    shard, other_shard, checkpoint = '00', 'ff', '.collect.json'
    for path, raw in zip(paths, payloads):
        fao.write(path, raw)
    # Keep a reachable child as well as the SQL root itself.
    (version, record), = json.loads(payloads[0])['entries'].items()
    publication = file_map._write(data_root, {'children': {file_map._key(version)[0]: paths[0].stem}})
    if domain == 'archive':
        conn.execute("INSERT INTO legacy_archive_heads VALUES ('album',?)", (bytes.fromhex(publication),))
    else:
        conn.execute("INSERT INTO settings VALUES (?,json_array(1,1,'archived',NULL,NULL,NULL,?))",
                     (policy_history_read.PREFIX + version, publication))
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
        if domain == 'archive':
            result = legacy_journals.collect(conn, now, limit=16)
            assert all(reserved for _, reserved in examined)
        else:
            with files.history_garbage(policy_history_read.publication(conn), now - 30 * 86400, limit=16) as candidates:
                result = sum(files.remove_history(path, info) for path, info, _ in candidates)
        assert len(examined) <= 16 and len(opened) <= 16
        return result

    try:
        for _ in range(16):
            step(time.time())
        assert all(path.exists() for path in paths)  # Thirty-day orphan grace.
        with policy_documents.retained_reader(root):
            assert step(10**10) == 0
            assert not examined and not opened
        if domain == 'archive':
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
            if key[0] is conn or key[0] is files:
                file_collection._CURSORS.pop(key).close()
        conn.close()
        conn = sqlite3.connect(database, timeout=0)
        files = PolicyFiles(tmp_path / 'current', root)
        for _ in range(100):
            step(10**10)
            if not any(path.exists() for path in paths[1:] + extra_orphans):
                break
        assert all(not path.exists() for path in paths[1:])
        assert all(not path.exists() for path in extra_orphans)
        assert fao.read(paths[0]).bytes == payloads[0]
        assert fao.read(current_root).bytes == original_root
        if domain == 'policy':
            assert files.archived(version, policy_history_read.header(conn, version)) == record
        assert fao.read(nested / 'untouched.json').bytes == b'nested'
        saved = json.loads(fao.read(root / checkpoint).bytes)
        assert set(saved) == {'shard', 'after_shard', 'prefix'}
        # A superseded current body becomes collectible on a later wrap.
        if domain == 'archive':
            conn.execute('DELETE FROM legacy_archive_heads')
        else:
            conn.execute("UPDATE settings SET value=json_set(value,'$[2]','expired','$[6]',NULL) WHERE key=?",
                         (policy_history_read.PREFIX + version,))
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
