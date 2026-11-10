"""SQL facts and immutable receipts for recommendation policy documents."""
from yt_playlist.core.db_space import require_database_space
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.core.policy_config import PolicyCapacityFull
from yt_playlist.repos import policy_storage
from yt_playlist.repos.policy_storage import refresh_references, typed


class PolicyRepo(Repo):
    @lookup_read
    def file_owned(self):
        return typed(self.conn)

    @lookup_read
    def records(self, versions, bodies=()):
        result = {'policies': {}, 'bodies': {}}
        for name, table, keys in (('policies', 'recommendation_policies', versions),
                                   ('bodies', 'recommendation_policy_bodies', bodies)):
            keys = list(dict.fromkeys(keys))
            for offset in range(0, len(keys), 256):
                batch = keys[offset:offset + 256]
                result[name].update((row['version'], dict(row)) for row in self.conn.execute(
                    f'SELECT * FROM {table} WHERE version IN (' + ','.join('?' for _ in batch) + ')', batch))
        return result

    @lookup_read
    def archived(self, version):
        if not self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='policy_history_archive'").fetchone():
            return None
        row = self.conn.execute('SELECT state FROM recommendation_policy_history WHERE version=?', (version,)).fetchone()
        if row is None:
            return None
        head = self.conn.execute('SELECT publication FROM policy_history_archive WHERE id=1').fetchone()
        return {'state': row[0], 'publication': head[0].hex() if head else None}

    @lookup_read
    def legacy_capture(self, versions):
        import json
        from yt_playlist.core.policy_config import BASE_FIELD
        rows, bases = [], {}
        for version in sorted(set(versions) - {None}):
            row = self.conn.execute('SELECT * FROM rec_serving_policies WHERE model_version=?', (version,)).fetchone()
            if row is None:
                continue
            rows.append(tuple(row))
            base = json.loads(row['configuration']).get(BASE_FIELD)
            if base and base not in bases:
                body = self.conn.execute('SELECT base_version,configuration,byte_size,created_at FROM rec_policy_bases '
                    'WHERE base_version=?', (base,)).fetchone()
                if body is None:
                    raise ValueError(f'Missing captured policy body: {base}')
                bases[base] = tuple(body)
        return rows, list(bases.values())

    @synchronized
    def publish(self, records):
        """Recheck immutable identities before installing the SQL receipts."""
        with self.conn.atomic():
            pending = []
            for record in records:
                table = 'recommendation_policy_bodies' if record['body'] else 'recommendation_policies'
                row = self.conn.execute(f'SELECT content_digest FROM {table} WHERE version=?',
                                         (record['version'],)).fetchone()
                if row is not None:
                    if row[0] != record['digest']:
                        raise ValueError(f"Conflicting immutable recommendation policy: {record['version']!r}")
                    continue
                pending.append(record)
            require_database_space(self.conn, 2 * sum(record['size'] for record in pending))
            for record in pending:
                columns = 'version,configuration_filename,content_digest,byte_size,configuration_offset,created_at'
                values = (record['version'], record['filename'], record['digest'], record['size'],
                          record['offset'], record['created'])
                table = 'recommendation_policy_bodies' if record['body'] else 'recommendation_policies'
                if not record['body']:
                    columns += ',base_version'
                    values += (record['base'],)
                self.conn.execute(f'INSERT INTO {table}({columns}) VALUES (' + ','.join('?' for _ in values) + ')', values)
            versions = [record['version'] for record in records if not record['body']]
            for offset in range(0, len(versions), 256):
                batch = versions[offset:offset + 256]
                self.conn.execute('UPDATE recommendation_impression_policies SET policy_version=raw_version,resolution_reason=NULL '
                    "WHERE resolution_reason='missing_policy' AND raw_version IN (" + ','.join('?' for _ in batch) + ')', batch)

    @lookup_read
    def unused_inventory(self):
        count, variants = self.conn.execute('SELECT count(*),COALESCE(sum(byte_size),0) '
            'FROM recommendation_policies WHERE served=0').fetchone()
        bodies = self.conn.execute('SELECT COALESCE(sum(byte_size),0) FROM recommendation_policy_bodies '
            'WHERE served=0').fetchone()[0]
        return count, variants + bodies

    @synchronized
    def publish_legacy(self, rows, bases, links, *, capacity=None):
        from yt_playlist.repos.policy_storage import _new_records
        with self.conn.atomic():
            require_database_space(self.conn, 2 * (sum(len(row[1].encode()) for row in rows) + sum(row[2] for row in bases)))
            if capacity is not None:
                new_rows = _new_records(self.conn, 'rec_serving_policies', 'model_version', rows)
                new_bases = _new_records(self.conn, 'rec_policy_bases', 'base_version', bases)
                size = sum(len(row[1].encode()) for row in new_rows) + sum(row[2] for row in new_bases)
                count, variants = self.conn.execute('SELECT count(*),COALESCE(sum(length(CAST(p.configuration AS BLOB))),0) '
                    'FROM rec_policy_variants v JOIN rec_serving_policies p USING(model_version) WHERE v.served=0').fetchone()
                bodies = self.conn.execute('SELECT COALESCE(sum(byte_size),0) FROM rec_policy_bases WHERE served=0').fetchone()[0]
                if count + len(new_rows) > capacity[0] or bodies + variants + size > capacity[1]:
                    raise PolicyCapacityFull(count, bodies + variants, len(new_rows), size)
            self.conn.executemany('INSERT OR IGNORE INTO rec_policy_bases(base_version,configuration,byte_size,created_at) '
                'VALUES (?,?,?,?)', bases)
            self.conn.executemany('INSERT OR IGNORE INTO rec_serving_policies VALUES (?,?,?)', rows)
            self.conn.executemany('INSERT OR IGNORE INTO rec_policy_variants(model_version,base_version) VALUES (?,?)', links)
            refresh_references(self.conn, [version for version, _base in links])

    @snapshot_read
    def legacy_root_messages(self):
        return policy_storage.legacy_root_messages(self.conn)

    @snapshot_read
    def retention_candidates(self, before, *, protected, limit, byte_limit, cursor_key):
        return policy_storage.retention_candidates(self.conn, before, protected=protected, limit=limit,
                                                   byte_limit=byte_limit, cursor_key=cursor_key)

    @lookup_read
    def retirement_pending(self, before, *, protected):
        return policy_storage.retirement_pending(self.conn, before, protected=protected)

    @synchronized
    def clear_retirement_cursor(self, cursor_key):
        with self.conn.atomic():
            self.conn.execute('DELETE FROM settings WHERE key=?', (cursor_key,))

    @synchronized
    def prune(self, versions, cursor, *, cursor_key, limit, byte_limit):
        with self.conn.atomic():
            return policy_storage.prune(self.conn, versions, cursor, cursor_key=cursor_key,
                                        limit=limit, byte_limit=byte_limit)

    @lookup_read
    def history_available(self):
        return self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='policy_history_archive'").fetchone() is not None

    @lookup_read
    def history_publication(self):
        row = self.conn.execute('SELECT publication FROM policy_history_archive WHERE id=1').fetchone()
        return row[0].hex() if row else None

    @snapshot_read
    def history_candidates(self, before, limit):
        return [row['version'] for row in policy_storage.history_candidates(self.conn, before, limit)]

    @lookup_read
    def history_records(self, before, versions):
        rows = policy_storage.history_candidates(self.conn, before, len(versions), versions=versions)
        bodies = {row['base_version'] for row in rows if row['base_version'] is not None}
        return rows, self.records((), bodies)['bodies']

    @synchronized
    def publish_history(self, publication, versions):
        with self.conn.atomic():
            self.conn.execute('INSERT INTO policy_history_archive VALUES (1,?) '
                'ON CONFLICT(id) DO UPDATE SET publication=excluded.publication', (bytes.fromhex(publication),))
            trigger = self.conn.execute("SELECT sql FROM sqlite_schema WHERE name='recommendation_impression_link_retained'").fetchone()
            if trigger:
                self.conn.execute('DROP TRIGGER recommendation_impression_link_retained')
            try:
                for version, created in versions:
                    self.conn.execute("INSERT INTO recommendation_policy_history VALUES (?,?,'archived') "
                        "ON CONFLICT(version) DO UPDATE SET state='archived'", (version, created))
                    self.conn.execute('INSERT INTO historical_impression_policies SELECT impression_id,kind,policy_version '
                        'FROM recommendation_impression_policies WHERE policy_version=? '
                        'ON CONFLICT(impression_id,kind) DO NOTHING', (version,))
                    self.conn.execute('DELETE FROM recommendation_impression_policies WHERE policy_version=?', (version,))
                    self.conn.execute('DELETE FROM recommendation_policies WHERE version=?', (version,))
            finally:
                if trigger:
                    self.conn.execute(trigger[0])

    @lookup_read
    def expiring_history(self, before, limit):
        return [row[0] for row in self.conn.execute("SELECT version FROM recommendation_policy_history WHERE state='archived' "
            'AND created_at<? ORDER BY created_at,version LIMIT ?', (before, limit))]

    @synchronized
    def expire_history(self, publication, versions):
        with self.conn.atomic():
            self.conn.execute('UPDATE policy_history_archive SET publication=? WHERE id=1', (bytes.fromhex(publication),))
            self.conn.executemany("UPDATE recommendation_policy_history SET state='expired' WHERE version=?",
                                  [(version,) for version in versions])
