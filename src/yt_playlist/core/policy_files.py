"""Database-free access to live and archived recommendation policy files."""
from contextlib import ExitStack, contextmanager
import hashlib

from yt_playlist.core import fao, file_collection, file_map, policy_documents, policy_history_files
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.policy_paths import record_path, record_id


class PolicyFiles:
    def __init__(self, root, archive_root):
        self.root, self.archive_root = root, archive_root

    @contextmanager
    def retained(self):
        with ExitStack() as stack:
            stack.enter_context(file_collection.retained_reader(self.root))
            stack.enter_context(file_collection.retained_reader(self.archive_root))
            yield

    @contextmanager
    def write_reserved(self, records):
        references = []
        with ExitStack() as retained:
            for record in records:
                path = record_path(self.root, record['file_id'])
                encoded = canonical({'format': policy_documents.FORMAT, 'configuration': record['value']})
                if (len(encoded) > policy_documents.MAX_BYTES or len(encoded) != record['size']
                        or hashlib.sha256(encoded).hexdigest() != record['digest']):
                    raise ValueError('Policy configuration changed after reservation')
                pending = retained.enter_context(fao.prepare(path, fao.Content(encoded, max_bytes=policy_documents.MAX_BYTES)))
                pending.publish()
                references.append((str(path), record['digest'], len(encoded), 0))
            yield references

    def read(self, row):
        return policy_documents.read(row['configuration_filename'], row['content_digest'],
            offset=row['configuration_offset'], size=row['byte_size'])

    def archived(self, version, header):
        if header is None or header['state'] == 'expired':
            return None
        if header.get('file') is not None:
            return policy_history_files.read(self.archive_root, version, header['file'])
        value = file_map.get(self.archive_root, header['publication'], version)
        if value is None or value.get('state') != 'archived':
            raise fao.CorruptFile('Required historical policy is missing')
        return value

    def archive_records(self, rows, bodies):
        records, used = [], 0
        for row in rows:
            value = self.read(row)
            if value.get(BASE_FIELD) != row['base_version']:
                raise ValueError('Policy configuration does not match its shared-body reference')
            base = None
            if row['base_version'] is not None:
                source = bodies.get(row['base_version'])
                if source is None:
                    raise fao.CorruptFile('Required shared historical policy is missing')
                base = {'version': source['version'], 'created_at': source['created_at'], 'configuration': self.read(source)}
            saved = {'state': 'archived', 'created_at': row['created_at'], 'configuration': value, 'base': base}
            encoded = policy_history_files.encode(row['version'], saved)
            size = len(encoded)
            if records and used + size > file_map.MAX_WORK // 4:
                break
            records.append(dict(version=row['version'], created=row['created_at'], value=saved,
                                digest=hashlib.sha256(encoded).hexdigest(), size=size))
            used += size
        return records

    @contextmanager
    def write_archive_reserved(self, records):
        with ExitStack() as retained:
            for record in records:
                encoded = policy_history_files.encode(record['version'], record['value'])
                if len(encoded) != record['size'] or hashlib.sha256(encoded).hexdigest() != record['digest']:
                    raise ValueError('Historical policy changed after reservation')
                path = record_path(self.archive_root, record['file_id'])
                pending = retained.enter_context(fao.prepare(path, fao.Content(encoded, max_bytes=policy_history_files.MAX_BYTES)))
                pending.publish()
            yield

    @contextmanager
    def history_garbage(self, publication, before, *, limit):
        def shard(name):
            return isinstance(name, str) and len(name) == 2 and all(c in '0123456789abcdef' for c in name)

        root = self.archive_root
        with file_collection.collection(self, root, name='.collect.json', valid_shard=shard, prefixes=('', 'by-id')) as work, \
                file_collection.collector(root) as admitted:
            if not admitted:
                yield iter(())
                return
            budget = file_map.Budget()

            def candidates():
                for _ in range(limit):
                    path = work.next()
                    if path is None:
                        continue
                    identity = record_id(path)
                    if identity is None and (path.suffix != '.json' or len(path.stem) != 64
                            or any(c not in '0123456789abcdef' for c in path.stem) or path.stem[:2] != path.parent.name):
                        continue
                    info = fao.read(path).info
                    if info.ctime_ns / 1e9 <= before and (identity is not None
                            or not file_map.contains_page(root, publication, path.stem, budget)):
                        yield path, info, identity

            yield candidates()
            work.save()

    @staticmethod
    def remove_history(path, info):
        return fao.delete(path, expected=info.identity, missing_ok=True)
