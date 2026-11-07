"""Database-free access to live and archived recommendation policy files."""
from contextlib import ExitStack, contextmanager

from yt_playlist.core import fao, file_collection, file_map, policy_documents
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.policy_config import BASE_FIELD


class PolicyFiles:
    def __init__(self, root, archive_root):
        self.root, self.archive_root = root, archive_root

    @contextmanager
    def retained(self):
        with ExitStack() as stack:
            stack.enter_context(file_collection.retained_reader(self.root))
            stack.enter_context(file_collection.retained_reader(self.archive_root))
            yield

    def publish_many(self, values):
        return list(policy_documents.publish_many(self.root, values))

    def read(self, row):
        return policy_documents.read(row['configuration_filename'], row['content_digest'],
            offset=row['configuration_offset'], size=row['byte_size'])

    def archived(self, version, header):
        if header is None or header['state'] == 'expired':
            return None
        value = file_map.get(self.archive_root, header['publication'], version)
        if value is None or value.get('state') != 'archived':
            raise fao.CorruptFile('Required historical policy is missing')
        return value

    def _history_changes(self, publication, changes):
        changes = file_map.bounded_changes(self.archive_root, publication, changes)
        publication = file_map.update(self.archive_root, publication, changes)
        if file_map.get_many(self.archive_root, publication, changes) != changes:
            raise fao.CorruptFile('Historical policy publication changed the original configuration')
        return publication, changes

    def archive_many(self, publication, rows, bodies):
        changes, used = {}, 0
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
            size = len(canonical({row['version']: saved}))
            if changes and used + size > file_map.MAX_WORK // 4:
                break
            changes[row['version']] = saved
            used += size
        if not changes:
            return publication, []
        publication, changes = self._history_changes(publication, changes)
        return publication, [(version, value['created_at']) for version, value in changes.items()]

    def expire_history(self, publication, versions):
        publication, changes = self._history_changes(publication, {version: {'state': 'expired'} for version in versions})
        return publication, list(changes)

    def collect_history(self, publication, before, *, limit):
        def shard(name):
            return isinstance(name, str) and len(name) == 2 and all(c in '0123456789abcdef' for c in name)

        root = self.archive_root
        with file_collection.collection(self, root, name='.collect.json', valid_shard=shard) as work, \
                file_collection.collector(root) as admitted:
            if not admitted:
                return 0
            budget, removed = file_map.Budget(), 0
            for _ in range(limit):
                path = work.next()
                if (path is None or path.suffix != '.json' or len(path.stem) != 64
                        or any(c not in '0123456789abcdef' for c in path.stem) or path.stem[:2] != path.parent.name):
                    continue
                info = fao.read(path).info
                if info.ctime_ns / 1e9 <= before and not file_map.contains_page(root, publication, path.stem, budget):
                    removed += fao.delete(path, expected=info.identity, missing_ok=True)
            work.save()
            return removed
