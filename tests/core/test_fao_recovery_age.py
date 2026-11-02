"""Domain recovery must preserve original file clocks, including repacked ranges.

These small SQLite owners isolate the file codecs. Full domain schema/guard and
workflow acceptance remains covered by each owner's existing recovery checks.
"""
import os
import hashlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core import (action_backup_files, album_description_files, audio_annotation_files,
    collection_recovery, fao, group_documents, legacy_evidence,
    metadata_delivery_files, policy_documents, recommendation_outputs)


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
    elif domain == 'annotations':
        module = audio_annotation_files
        conn.execute('CREATE TABLE audio_annotations(id INTEGER PRIMARY KEY,model_output_filename TEXT)')
        for i in range(3):
            filename = module.publish(module.directory(conn), {'format': module.FILE_FORMAT, 'saved': i})
            conn.execute('INSERT INTO audio_annotations VALUES (?,?)', (i, filename))
        def records(db):
            for key, filename in db.execute('SELECT * FROM audio_annotations'):
                yield key, filename, module.read(filename)
    elif domain == 'descriptions':
        module = album_description_files
        for table, (keys, filename, digest) in module.OWNERS.items():
            conn.execute(f'CREATE TABLE {table}(' + ','.join(key + ' TEXT' for key in keys)
                         + f',{filename} TEXT,{digest} TEXT)')
        for i in range(3):
            filename, digest, _size = module.publish(module.directory(conn), '{ "saved": ' + str(i) + ' }')
            conn.execute('INSERT INTO album_descriptions VALUES (?,?,?)', (str(i), filename, digest))
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
        conn.execute('CREATE TABLE action_backups(action_id INTEGER,position INTEGER,filename TEXT,retained_filename TEXT,'
                     'digest TEXT,bytes INTEGER,availability TEXT)')
        conn.execute('CREATE TABLE action_steps(action_id INTEGER,backup_filename TEXT,backup_digest TEXT,'
                     'backup_bytes INTEGER,backup_status TEXT)')
        for i in range(3):
            encoded = ('{ "tracks": [], "ytm_playlist_id": "source-' + str(i) + '" }').encode()
            digest = hashlib.sha256(encoded).hexdigest()
            filename = policy_documents._publish_encoded(collection_recovery.directory(conn), encoded, digest)
            conn.execute('INSERT INTO action_backups VALUES (?,?,?,?,?,?,?)',
                         (i, 0, filename, filename, digest, len(encoded), 'verified'))
        def records(db):
            for row in module.inventory(db):
                yield row['action_id'], row['retained_filename'], module.verified(row['retained_filename'])[0]
    else:
        assert domain == 'collections'
        module = collection_recovery
        conn.execute('CREATE TABLE road_trip_drafts(id INTEGER PRIMARY KEY,recovery_filename TEXT,recovery_digest TEXT,'
                     'recovery_bytes INTEGER)')
        for i in range(3):
            conn.execute('INSERT INTO road_trip_drafts VALUES (?,?,?,?)',
                         (i, *module.publish(conn, 'road-trip-draft', i, {'saved': i})))
        def records(db):
            for owner, key, kind, filename, digest, size in module.references(db):
                yield (owner, key), filename, module.read(filename, digest, size, kind=kind, owner=key)
    conn.commit()
    return module, records, getattr(module, 'directory', collection_recovery.directory)


@pytest.mark.parametrize('domain', ['policies', 'groups', 'metadata', 'archives',
                                    'annotations', 'descriptions', 'outputs', 'collections', 'actions'])
@pytest.mark.parametrize('compressed', [False, True])
def test_recovery_preserves_each_owned_records_original_clocks(tmp_path, domain, compressed):
    _check_recovery(tmp_path, domain, compressed)


@pytest.mark.parametrize('domain', ['policies', 'groups', 'metadata', 'archives'])
@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_equal_retained_payloads_preserve_distinct_source_clocks(tmp_path, domain, compressed, reverse):
    _check_recovery(tmp_path, domain, compressed, duplicate=True, reverse=reverse)


def _check_recovery(tmp_path, domain, compressed, *, duplicate=False, reverse=False):
    with closing(sqlite3.connect(tmp_path / 'source.db')) as source, closing(sqlite3.connect(tmp_path / 'restored.db')) as target:
        source.row_factory = target.row_factory = sqlite3.Row
        module, records, directory = _seed(source, domain, duplicate=duplicate)
        source_root = directory(source)
        paths = sorted({Path(filename) for _key, filename, _value in records(source)})
        if duplicate:
            assert len(paths) == 2
            assert len({repr(value) for _key, _filename, value in records(source)}) == 1
        for index, path in enumerate(paths):
            if reverse:
                index = len(paths) - index - 1
            encoded = fao.read_bytes(path)
            # Distinct ages challenge merging records with different retention clocks.
            age = (1_000_000_000_000 + index * 1_000_000_000, 2_000_000_000_000 + index * 1_000_000_000)
            if compressed:
                fao.write_bytes(path, encoded, age=replace(fao.stat(path), mtime_ns=age[0], ctime_ns=age[1]))
            else:
                path.write_bytes(encoded)
                os.utime(path, ns=(age[0], age[0]))

        def snapshot(db):
            result = {}
            for key, filename, value in records(db):
                info = fao.stat(filename)
                result[key] = value, (info.mtime_ns, info.ctime_ns)
            return result

        expected = snapshot(source)
        with policy_documents.retained_reader(source_root):
            source.execute('BEGIN')
            try:
                manifest = module.export(source, tmp_path / 'backup')
                source.backup(target)
            finally:
                source.rollback()
        source_root.rename(tmp_path / 'hidden-producer')
        module.restore(target, manifest)
        assert snapshot(target) == expected
        # Recovery keeps compact packs rather than allocating a file per range.
        assert len({filename for _key, filename, _value in records(target)}) <= len(paths)
        assert all(Path(filename).is_relative_to(directory(target)) for _key, filename, _value in records(target))
        assert target.execute('PRAGMA foreign_key_check').fetchall() == []
        # A previous copy with fresh clocks must not defeat a resumed restore.
        for filename in {filename for _key, filename, _value in records(target)}:
            fao.write_bytes(filename, fao.read_bytes(filename))
        assert snapshot(target) != expected
        module.restore(target, manifest)
        assert snapshot(target) == expected
