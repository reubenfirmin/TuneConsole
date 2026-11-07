"""Independent disposable library fixtures using the current domain exporters.

No schema upgrade, retention policy, compaction or installation runs here.
Production rebuilds keep installed file roots in place instead.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from yt_playlist.core import (action_backup_files, album_description_files, audio_annotation_files,
    choice_evidence_files, collection_recovery, fao, file_map, file_owners, group_documents,
    legacy_evidence, metadata_delivery_files, metadata_destination_files, metadata_runtime,
    metadata_runtime_files, policy_documents, preparation_files, recommendation_outputs,
    sample_files, saved_preparation_files, served_facts)
from yt_playlist.core.db_rebuild import job
from yt_playlist.core.db_rebuild.sqlite_checks import tables, verify


class FileInventory:
    """Explicit fixture inventory; no backup protocol in the production FAO."""

    @staticmethod
    def export(root, paths, target):
        root, target = Path(root).resolve(), Path(target).resolve()
        if root == target:
            raise ValueError('Managed backup needs a separate destination')
        entries = []
        for source in sorted(set(map(Path, paths))):
            relative = source.resolve().relative_to(root)
            destination = target / relative
            fao.write(destination, fao.read(source))
            size, digest = fao.read(destination).digest
            entries.append([relative.as_posix(), size, digest])
        manifest = target / 'manifest.json'
        fao.write(manifest, json.dumps({'format': 'fao-backup-1', 'files': entries},
                                      separators=(',', ':')).encode())
        return str(manifest)

    @staticmethod
    def restore(root, manifest, *, expected):
        root, manifest = Path(root).resolve(), Path(manifest)
        document = json.loads(fao.read(manifest).bytes)
        if (not isinstance(document, dict) or set(document) != {'format', 'files'}
                or document['format'] != 'fao-backup-1' or not isinstance(document['files'], list)):
            raise fao.CorruptFile('Unsupported managed backup')
        required = {Path(path).resolve().relative_to(root).as_posix() for path in expected}
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.fao-restore-', dir=root) as workspace:
            admitted = {}
            for name, size, digest in document['files']:
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts or name not in required or name in admitted:
                    raise fao.CorruptFile('Managed backup differs from its domain owners')
                staged = Path(workspace) / relative
                fao.write(staged, fao.read(manifest.parent / relative))
                admitted[name] = staged
                if fao.read(staged).digest != (size, digest):
                    raise fao.CorruptFile('Managed backup differs from its content digest')
            if admitted.keys() != required:
                raise fao.CorruptFile('Managed backup lacks required domain files')
            for name, staged in admitted.items():
                fao.write(root / name, fao.read(staged))


def export_owner(conn, module, root):
    return FileInventory.export(module.directory(conn), module.references(conn), root)


def restore_owner(conn, name, module, manifest):
    root, saved = module.directory(conn), Path(manifest).resolve().parent
    if name == 'legacy-journals':
        expected = (root / path.relative_to(saved)
                    for domain, publication in conn.execute('SELECT domain,publication FROM legacy_archive_heads')
                    for path in file_map.pages(saved / domain, publication.hex()))
    elif name == 'policy-history':
        expected = (root / path.relative_to(saved) for path in module.references(conn, root=saved))
    else:
        expected = module.references(conn)
    FileInventory.restore(root, manifest, expected=expected)


class PolicyFiles:
    """Policy relocation used only while constructing disposable test libraries."""

    @staticmethod
    def export(conn, root):
        """Export under retained_reader, pinned to the same snapshot as the DB backup."""
        if not conn.in_transaction:
            raise ValueError('Policy export requires a pinned database snapshot')
        if Path(root).resolve() == policy_documents.directory(conn).resolve():
            raise ValueError('Policy backup requires a separate directory')
        entries, chunks, pending = [], [], []
        used = 0

        def copy_batch():
            values = policy_documents.read_many(((filename,digest,offset,size)
                               for _owner,_version,filename,digest,offset,size in pending),with_age=True)
            for (owner, version, _filename, digest, _offset, size), target in zip(
                    pending, policy_documents.repack(root, values, policy_documents.publish_many)):
                filename, copied, copied_size, offset = target
                if (copied, copied_size) != (digest, size):
                    raise ValueError('Policy backup changed a configuration')
                entries.append({'owner': owner, 'version': version, 'digest': digest,
                                'relative_filename': str(Path(filename).relative_to(root)),
                                'offset': offset, 'byte_size': size})
                if len(entries) == 1024:
                    chunks.append(policy_documents.publish(root, {'type': 'backup-chunk', 'policies': entries})[1])
                    entries.clear()

        for owner in policy_documents.OWNERS:
            for version, filename, digest, offset, size in conn.execute(
                    f'SELECT version,configuration_filename,content_digest,configuration_offset,byte_size '
                    f'FROM {owner} ORDER BY configuration_filename,configuration_offset,version'):
                if pending and used + size > policy_documents.PACK_BYTES:
                    copy_batch()
                    pending, used = [], 0
                pending.append((owner, version, filename, digest, offset, size))
                used += size
        if pending:
            copy_batch()
        if entries:
            chunks.append(policy_documents.publish(root, {'type': 'backup-chunk', 'policies': entries})[1])
        return policy_documents.publish(root, {'type': 'backup-manifest', 'chunks': chunks})[0]


    @staticmethod
    def restore(conn, manifest, *, memory_key=None):
        path = Path(manifest)
        document = policy_documents.read(path, path.stem)
        if (set(document) != {'type', 'chunks'} or document['type'] != 'backup-manifest'
                or not isinstance(document['chunks'], list)):
            raise ValueError('Unsupported policy backup manifest')
        entries = []
        for digest in document['chunks']:
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Invalid policy backup chunk digest')
            chunk = policy_documents.read(path.parent.parent / digest[:2] / (digest + '.json'), digest)
            if (set(chunk) != {'type', 'policies'} or chunk['type'] != 'backup-chunk'
                    or not isinstance(chunk['policies'], list)):
                raise ValueError('Unsupported policy backup chunk')
            entries.extend(chunk['policies'])
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {
                    'owner', 'version', 'digest', 'relative_filename', 'offset', 'byte_size'}
                    or entry['owner'] not in policy_documents.OWNERS or not isinstance(entry['version'], str)
                    or not entry['version'] or not isinstance(entry['digest'], str)
                    or len(entry['digest']) != 64 or any(c not in '0123456789abcdef' for c in entry['digest'])
                    or type(entry['offset']) is not int or entry['offset'] < len(policy_documents.PACK_HEADER)
                    or type(entry['byte_size']) is not int or not 0 < entry['byte_size'] <= policy_documents.MAX_BYTES
                    or not isinstance(entry['relative_filename'], str)):
                raise ValueError('Invalid policy backup entry')
            relative = Path(entry['relative_filename'])
            stem = relative.stem
            if (len(stem) != 64 or any(c not in '0123456789abcdef' for c in stem)
                    or relative != Path(stem[:2]) / (stem + '.json')
                    or entry['relative_filename'] != str(relative)):
                raise ValueError('Invalid policy backup path')
        if len({(entry['owner'], entry['version']) for entry in entries}) != len(entries):
            raise ValueError('Duplicate policy in backup manifest')
        if conn.in_transaction:
            raise ValueError('Policy restore requires its own writer reservation')
        conn.execute('BEGIN IMMEDIATE')
        try:
            expected = {(owner, version): (digest, size) for owner in policy_documents.OWNERS
                        for version, digest, size in conn.execute(f'SELECT version,content_digest,byte_size FROM {owner}')}
            if expected != {(entry['owner'], entry['version']): (entry['digest'], entry['byte_size'])
                            for entry in entries}:
                raise ValueError('Policy backup does not match the restored database')
            targets = []
            root = policy_documents.directory(conn, memory_key)
            values = policy_documents.read_many(((path.parent.parent/entry['relative_filename'],entry['digest'],
                                 entry['offset'],entry['byte_size']) for entry in entries),with_age=True)
            for entry, (filename, copied, size, offset) in zip(entries, policy_documents.repack(root, values, policy_documents.publish_many)):
                if (copied, size) != (entry['digest'], entry['byte_size']):
                    raise ValueError('Policy restore changed a configuration')
                targets.append((entry['owner'], filename, offset, entry['version']))
            with policy_documents.coordinate_updates(conn, policy_documents.OWNERS):
                for owner in policy_documents.OWNERS:
                    conn.executemany(f'UPDATE {owner} SET configuration_filename=?,configuration_offset=? WHERE version=?',
                                     [(filename, offset, version) for table, filename, offset, version in targets if table == owner])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise



def domains(conn):
    schema = tables(conn)
    yield 'choice_evidence', choice_evidence_files
    if served_facts.available(conn):
        yield 'served', served_facts
    if metadata_runtime.active(conn):
        yield 'runtime_metadata', metadata_runtime_files
    for table, name, module in (
        ('sample_attributes', 'samples', sample_files),
        ('action_backups', 'actions', action_backup_files),
        ('album_feedback', 'collections', collection_recovery),
        ('audio_annotations', 'annotations', audio_annotation_files),
        ('recommendation_policies', 'policies', PolicyFiles),
        ('album_description_requests', 'descriptions', album_description_files),
        ('group_versions', 'groups', group_documents),
        ('prepared_outputs', 'recommendations', recommendation_outputs),
        ('metadata_deliveries', 'metadata', metadata_delivery_files),
        ('published_metadata_destination_deliveries', 'metadata_preparation', metadata_destination_files),
    ):
        if table in schema:
            yield name, module
    if saved_preparation_files.available(conn):
        yield 'saved_preparation', saved_preparation_files
    if preparation_files.available(conn):
        yield 'preparation', preparation_files
    if 'legacy_catalog_evidence' in schema:
        yield 'archives', legacy_evidence


def export_files(conn, target, *, backup_root=None):
    target = Path(target)
    def root(domain):
        return (Path(backup_root) / domain if backup_root is not None else
                target.with_name(target.name + '.' + domain))
    manifests = {name: export_owner(conn, module, root(name))
                 for name, module in file_owners.owners().items() if module.available(conn)}
    for name, module in domains(conn):
        manifests[name] = module.export(conn, root(name))
    return manifests


def restore_files(conn, manifests, *, release=None):
    modules = dict(domains(conn))
    unknown = set(manifests) - set(file_owners.owners()) - set(modules)
    if unknown:
        raise ValueError('Unsupported fixture file domain: ' + repr(sorted(unknown)))
    for name, module in file_owners.owners().items():
        if name in manifests:
            restore_owner(conn, name, module, manifests[name])
    if release is not None:
        for name in file_owners.owners():
            if name in manifests:
                release(name)
    for name, module in modules.items():
        if name in manifests:
            module.restore(conn, manifests[name])
            if release is not None:
                release(name)


def snapshot_library(source, root, *, restore=True):
    """Export an exact snapshot; optionally restore it into a second library."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    snapshot, restored = root / 'snapshot.db', root / 'restored.db'
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        with job._files(reader):
            reader.execute('BEGIN')
            try:
                manifests = export_files(reader, snapshot)
                with closing(sqlite3.connect(snapshot)) as copied:
                    reader.backup(copied)
                    verify(copied)
            finally:
                reader.rollback()
    if restore:
        with closing(sqlite3.connect(snapshot)) as reader, closing(sqlite3.connect(restored)) as target:
            reader.backup(target)
            target.row_factory = sqlite3.Row
            target.execute('PRAGMA foreign_keys=ON')
            restore_files(target, manifests)
            verify(target)
    return {'snapshot': str(snapshot), 'restored': str(restored) if restore else None,
            'manifests': manifests}
