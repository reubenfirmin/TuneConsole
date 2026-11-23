"""Domain recovery must preserve original file clocks, including repacked ranges.

Most domains use small SQLite owners to isolate the file codecs. Collection
recovery uses the shipped schema to cover references across its live owners.
"""
import os
import hashlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from tests.file_custody_support import PolicyFiles

from yt_playlist.core import (action_backup_files, action_backup_inventory, album_description_files,
    collection_recovery, fao, group_documents, legacy_evidence,
    metadata_delivery_files, policy_documents, recommendation_outputs, sync_state)


def _seed(conn, domain, *, duplicate=False):
    """Return the real exporter and (owner key, filename, decoded value) reader."""
    if domain == 'policies':
        module = policy_documents
        for table in module.OWNERS:
            conn.execute(f'CREATE TABLE {table}(version TEXT PRIMARY KEY,configuration_filename TEXT,'
                         'content_digest TEXT,byte_size INTEGER,configuration_offset INTEGER)')
        for batch in range(2):
            values = [{'saved': f'{batch}-{i}', 'unicode': 'é'} for i in range(3)]
            if duplicate:
                values[0] = {'saved': 'same', 'unicode': 'é'}
            for i, (filename, digest, size, offset) in enumerate(module.publish_many(module.directory(conn), values)):
                if duplicate and i:
                    continue
                conn.execute('INSERT INTO recommendation_policies VALUES (?,?,?,?,?)',
                             (f'{batch}-{i}', filename, digest, size, offset))
        def records(db):
            for table in module.OWNERS:
                for key, filename, digest, size, offset in db.execute(f'SELECT * FROM {table}'):
                    yield (table, key), filename, module.read(filename, digest, size=size, offset=offset)
    elif domain == 'groups':
        module = group_documents
        for table, keys, prefix, _kind in module.OWNERS:
            conn.execute(f'CREATE TABLE {table}(' + ','.join(key + ' ' + ('TEXT' if table == 'group_lineage' else 'INTEGER')
                         for key in keys) + f',{prefix}_filename TEXT,{prefix}_digest TEXT,{prefix}_bytes INTEGER,'
                         f'{prefix}_offset INTEGER)')
        for batch in range(2):
            values = [('definition', {'saved': f'{batch}-{i}'}) for i in range(3)]
            if duplicate:
                values[0] = ('definition', {'saved': 'same'})
            for i, reference in enumerate(module.publish_many(module.directory(conn), values)):
                if duplicate and i:
                    continue
                conn.execute('INSERT INTO group_versions VALUES (?,?,?,?,?)', (batch * 3 + i, *reference))
        def records(db):
            for table, key, kind, filename, digest, size, offset in module.references(db):
                yield (table, key), filename, module.read(filename, digest, size, offset, kind)
    elif domain == 'metadata':
        module = metadata_delivery_files
        conn.execute('CREATE TABLE metadata_deliveries(id INTEGER PRIMARY KEY,raw_filename TEXT,raw_digest TEXT,'
                     'raw_byte_size INTEGER,raw_offset INTEGER)')
        for batch in range(2):
            values = [module.encode({'saved': f'{batch}-{i}'}) for i in range(3)]
            if duplicate:
                values[0] = module.encode({'saved': 'same'})
            for i, reference in enumerate(module.publish_encoded(module.directory(conn), values)):
                if duplicate and i:
                    continue
                conn.execute('INSERT INTO metadata_deliveries VALUES (?,?,?,?,?)', (batch * 3 + i, *reference))
        def records(db):
            for key, filename, digest, size, offset in module.references(db):
                yield key, filename, module.read(filename, digest, size, offset)
    elif domain == 'archives':
        module = legacy_evidence
        for table in module.DOMAINS.values():
            conn.execute(f'CREATE TABLE {table}(kind TEXT,source_id TEXT,content_digest TEXT,byte_size INTEGER,'
                         'file_offset INTEGER,filename TEXT,PRIMARY KEY(kind,source_id))')
        for batch in range(2):
            values = [module._encode('catalog', f'original text {batch}-{i}') for i in range(3)]
            if duplicate:
                values[0] = module._encode('catalog', 'same original text')
            for i, (filename, digest, size, offset) in enumerate(module._publish(module.directory(conn) / 'catalog', 'catalog', values)):
                if duplicate and i:
                    continue
                conn.execute('INSERT INTO legacy_catalog_evidence VALUES (?,?,?,?,?,?)',
                             ('library', f'{batch}-{i}', digest, size, offset, filename))
        def records(db):
            for owner, (kind, key, digest, size, offset, filename) in module._owners(db):
                yield (owner, kind, key), filename, module._read(owner, filename, digest, size, offset)[1]
    elif domain == 'descriptions':
        module = album_description_files
        conn.execute('CREATE TABLE enrichment_provider_config(provider TEXT PRIMARY KEY,description_access TEXT)')
        conn.execute('CREATE TABLE album_description_requests('
                     'usage_id INTEGER PRIMARY KEY,evidence_filename TEXT,evidence_digest TEXT)')
        for i in range(3):
            filename, digest, _size = module.publish(module.directory(conn), '{ "saved": ' + str(i) + ' }')
            conn.execute('INSERT INTO album_description_requests VALUES (?,?,?)', (i, filename, digest))
        filename, digest, _size = module.publish(module.directory(conn), '{ "status": "blocked" }')
        conn.execute('''INSERT INTO enrichment_provider_config VALUES ('deepseek',json_object(
            'remote:test-credential',json_object('evidence_filename',?,'evidence_digest',?)))''', (filename, digest))
        def records(db):
            for owner, key, filename, digest in module._references(db):
                yield (owner, key), filename, module.read(filename, digest)
    elif domain == 'outputs':
        module = recommendation_outputs
        conn.execute('CREATE TABLE prepared_outputs(id INTEGER PRIMARY KEY,surface TEXT,chunk_key TEXT,filename TEXT,'
                     'content_digest TEXT,byte_size INTEGER)')
        for i in range(3):
            reference = module.publish(module.directory(conn), module.encode('home', str(i), {'saved': i}))
            conn.execute('INSERT INTO prepared_outputs VALUES (?,?,?,?,?,?)', (i + 1, 'home', str(i), *reference))
        def records(db):
            for owner, key, surface, chunk, filename, digest, size in module.references(db):
                yield (owner, key), filename, module.read(filename, digest, size, surface=surface, chunk_key=chunk)
    elif domain == 'actions':
        module = action_backup_files
        conn.execute('CREATE TABLE actions(id INTEGER PRIMARY KEY,' + action_backup_inventory.DECLARATION + ')')
        conn.execute('CREATE TABLE action_steps(action_id INTEGER,backup_filename TEXT,backup_digest TEXT,'
                     'backup_bytes INTEGER,backup_status TEXT)')
        for i in range(3):
            encoded = ('{ "tracks": [], "ytm_playlist_id": "source-' + str(i) + '" }').encode()
            digest = hashlib.sha256(encoded).hexdigest()
            filename = policy_documents._publish_encoded(collection_recovery.directory(conn), encoded, digest)
            conn.execute('INSERT INTO actions(id) VALUES (?)', (i,))
            action_backup_inventory.append(conn, i, 0, (filename, digest, len(encoded), 'verified'), filename)
        def records(db):
            for row in module.inventory(db):
                yield row['action_id'], row['retained_filename'], module.verified(row['retained_filename'])[0]
    else:
        assert domain == 'collections'
        module = collection_recovery
        for i in range(3):
            draft_id = f'draft-{i}'
            reference = module.publish(conn, 'road-trip-draft', draft_id, {'saved': i})
            conn.execute('''INSERT INTO road_trip_recipes(
                name,own_pct,target_minutes,created_at,updated_at,draft_id,draft_revision,draft_state,
                draft_updated_at,recovery_filename,recovery_digest,recovery_bytes,draft_selections
            ) VALUES (?,50,60,1,1,?,'fixture-revision','ready',1,?,?,?,'[]')''',
                (f'Trip {i}', draft_id, *reference))
        sync_state.write(conn, {'status': 'complete', 'last_attempt_at': 2}, 2)

        def records(db):
            for owner, key, kind, filename, digest, size in module.references(db):
                yield (owner, key), filename, module.read(filename, digest, size, kind=kind, owner=key)
    conn.commit()
    return module, records, getattr(module, 'directory', collection_recovery.directory)


@pytest.mark.parametrize('domain', ['policies', 'groups', 'metadata', 'archives',
                                    'descriptions', 'outputs', 'collections', 'actions'])
@pytest.mark.parametrize('compressed', [False, True])
def test_recovery_preserves_each_owned_records_original_clocks(
        tmp_path, domain, compressed, _pristine_current_schema):
    _check_recovery(tmp_path, domain, compressed, collection_schema=_pristine_current_schema)


@pytest.mark.parametrize('domain', ['policies', 'groups', 'metadata', 'archives'])
@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_equal_retained_payloads_preserve_distinct_source_clocks(tmp_path, domain, compressed, reverse):
    _check_recovery(tmp_path, domain, compressed, duplicate=True, reverse=reverse)


def _check_recovery(tmp_path, domain, compressed, *, duplicate=False, reverse=False, collection_schema=None):
    with closing(sqlite3.connect(tmp_path / 'source.db')) as source, closing(sqlite3.connect(tmp_path / 'restored.db')) as target:
        source.row_factory = target.row_factory = sqlite3.Row
        if domain == 'collections':
            collection_schema.backup(source)
        module, records, directory = _seed(source, domain, duplicate=duplicate)
        recovery = PolicyFiles if domain == 'policies' else module
        source_root = directory(source)
        paths = sorted({Path(filename) for _key, filename, _value in records(source)})
        if duplicate:
            assert len(paths) == 2
            assert len({repr(value) for _key, _filename, value in records(source)}) == 1
        for index, path in enumerate(paths):
            if reverse:
                index = len(paths) - index - 1
            encoded = fao.read(path).bytes
            # Distinct ages challenge merging records with different retention clocks.
            age = (1_000_000_000_000 + index * 1_000_000_000, 2_000_000_000_000 + index * 1_000_000_000)
            if compressed:
                fao.write(path, fao.Content(encoded, metadata=replace(fao.read(path).info, mtime_ns=age[0], ctime_ns=age[1])))
            else:
                path.write_bytes(encoded)
                os.utime(path, ns=(age[0], age[0]))

        def snapshot(db):
            result = {}
            for key, filename, value in records(db):
                info = fao.read(filename).info
                result[key] = value, (info.mtime_ns, info.ctime_ns)
            return result

        expected = snapshot(source)
        if domain == 'collections':
            assert len(expected) == 4  # Three trip drafts and the sync attempt.
        with policy_documents.retained_reader(source_root):
            source.execute('BEGIN')
            try:
                manifest = recovery.export(source, tmp_path / 'backup')
                source.backup(target)
            finally:
                source.rollback()
        source_root.rename(tmp_path / 'hidden-producer')
        recovery.restore(target, manifest)
        assert snapshot(target) == expected
        # Recovery keeps compact packs rather than allocating a file per range.
        assert len({filename for _key, filename, _value in records(target)}) <= len(paths)
        assert all(Path(filename).is_relative_to(directory(target)) for _key, filename, _value in records(target))
        assert target.execute('PRAGMA foreign_key_check').fetchall() == []
        # A previous copy with fresh clocks must not defeat a resumed restore.
        for filename in {filename for _key, filename, _value in records(target)}:
            fao.write(filename, fao.read(filename).bytes)
        assert snapshot(target) != expected
        recovery.restore(target, manifest)
        assert snapshot(target) == expected
