"""SQL ownership of live and archived recommendation policy documents."""
import json

from yt_playlist.core.db_space import require_database_space
from yt_playlist.core import policy_bodies, policy_history_read as history, policy_impressions, worker_settings
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.repos import policy_storage


class PolicyRepo(Repo):
    @lookup_read
    def records(self, versions, bodies=()):
        variants = {}
        versions = list(dict.fromkeys(versions))
        for offset in range(0, len(versions), 256):
            batch = versions[offset:offset + 256]
            marks = ','.join('?' for _ in batch)
            variants.update((row['version'], dict(row)) for row in self.conn.execute(
                f'SELECT * FROM recommendation_policies WHERE version IN ({marks})', batch))
        shared = {}
        for version in dict.fromkeys(bodies):
            record = policy_bodies.read(self.conn, version)
            if record is not None:
                shared[version] = {'version': version, **record}
        return {'policies': variants, 'bodies': shared}

    @lookup_read
    def archived(self, version):
        return history.header(self.conn, version)

    @synchronized
    def publish(self, records):
        """Recheck immutable identities before installing their file references."""
        with self.conn.atomic():
            pending = []
            for record in records:
                if record['body']:
                    row = policy_bodies.read(self.conn, record['version'])
                else:
                    row = self.conn.execute('SELECT content_digest FROM recommendation_policies WHERE version=?',
                                            (record['version'],)).fetchone()
                if row is not None:
                    if row['content_digest'] != record['digest']:
                        raise ValueError(f"Conflicting immutable recommendation policy: {record['version']!r}")
                    continue
                pending.append(record)
            require_database_space(self.conn, 2 * sum(record['size'] for record in pending))
            for record in pending:
                columns = 'version,configuration_filename,content_digest,byte_size,configuration_offset,created_at'
                values = (record['version'], record['filename'], record['digest'], record['size'],
                          record['offset'], record['created'])
                if record['body']:
                    policy_bodies.save(self.conn, *values)
                else:
                    self.conn.execute(f'INSERT INTO recommendation_policies({columns},base_version) VALUES (?,?,?,?,?,?,?)',
                                      (*values, record['base']))
            versions = [record['version'] for record in records if not record['body']]
            for offset in range(0, len(versions), 256):
                batch = versions[offset:offset + 256]
                policy_impressions.resolve(self.conn, batch)

    @lookup_read
    def unused_inventory(self):
        count, variants = self.conn.execute('SELECT count(*),COALESCE(sum(byte_size),0) '
            'FROM recommendation_policies WHERE served=0').fetchone()
        bodies = self.conn.execute(f'SELECT COALESCE(sum(byte_size),0) FROM {policy_bodies.VIEW} '
                                   'WHERE served=0').fetchone()[0]
        return count, variants + bodies

    @snapshot_read
    def retention_candidates(self, before, *, limit, byte_limit):
        return policy_storage.retention_candidates(self.conn, before, limit=limit, byte_limit=byte_limit)

    @lookup_read
    def retirement_pending(self, before):
        return policy_storage.retirement_pending(self.conn, before)

    @synchronized
    def prune(self, versions, *, limit, byte_limit):
        with self.conn.atomic():
            return policy_storage.prune(self.conn, versions, limit=limit, byte_limit=byte_limit)

    @lookup_read
    def history_publication(self):
        return history.publication(self.conn)

    @snapshot_read
    def history_candidates(self, before, limit):
        return [row['version'] for row in policy_storage.history_candidates(self.conn, before, limit)]

    @lookup_read
    def history_records(self, before, versions):
        rows = policy_storage.history_candidates(self.conn, before, len(versions), versions=versions)
        bodies = {row['base_version'] for row in rows if row['base_version'] is not None}
        return rows, self.records((), bodies)['bodies']

    @synchronized
    def reserve_history(self, count):
        with self.conn.atomic():
            last = int(worker_settings.read(self.conn, history.NEXT_ARCHIVE_FILE, '0'))
            worker_settings.save(self.conn, history.NEXT_ARCHIVE_FILE, str(last + count))
            return range(last + 1, last + count + 1)

    @synchronized
    def publish_history(self, records, before):
        with self.conn.atomic():
            candidates = {row['version']: row['created_at'] for row in policy_storage.history_candidates(
                self.conn, before, len(records), versions=[record['version'] for record in records])}
            published = 0
            for record in records:
                version, created = record['version'], record['created']
                if candidates.get(version) != created:
                    continue
                previous = history.entry(self.conn, version)
                identity = previous['id'] if previous is not None else record['file_id']
                created = previous['created_at'] if previous is not None else created
                entry = [identity, created, 'archived', record['file_id'], record['digest'], record['size'], None]
                worker_settings.save(self.conn, history.PREFIX + version,
                                     json.dumps(entry, separators=(',', ':'), allow_nan=False))
                policy_impressions.archive(self.conn, version)
                self.conn.execute('DELETE FROM recommendation_policies WHERE version=?', (version,))
                published += 1
            return published

    @lookup_read
    def expiring_history(self, before, limit):
        return [row[0] for row in self.conn.execute(f"SELECT version FROM {history.VIEW} WHERE state='archived' "
                                                   'AND created_at<? ORDER BY created_at,version LIMIT ?', (before, limit))]

    @synchronized
    def expire_history(self, versions):
        with self.conn.atomic():
            self.conn.executemany("UPDATE settings SET value=json_set(value,'$[2]','expired','$[3]',NULL,"
                                  "'$[4]',NULL,'$[5]',NULL,'$[6]',NULL) WHERE key=?",
                                  ((history.PREFIX + version,) for version in versions))

    @lookup_read
    def history_file_required(self, identity):
        return self.conn.execute(f'SELECT 1 FROM {history.VIEW} WHERE archive_file_id=?', (identity,)).fetchone() is not None
