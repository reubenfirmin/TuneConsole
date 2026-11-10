"""Paid-request rows, guarded updates and immutable result-file references."""
from contextlib import contextmanager

from yt_playlist.core import albums
from yt_playlist.repos.base import Repo, lookup_read, synchronized


class AlbumDescriptionsRepo(Repo):
    @lookup_read
    def legacy_record(self, surface):
        import json
        row = self.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        return json.loads(row[0]) if row else None

    @synchronized
    def store_legacy_records(self, records, now):
        import json
        self.conn.executemany('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
            'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
            [(surface, json.dumps(value), now) for surface, value in records])

    @contextmanager
    def transaction(self):
        """Reserve this repository's writer; callers keep file I/O outside it."""
        with self._lock:
            if self.conn.in_transaction:
                raise ValueError('Paid request admission requires its own transaction')
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    @lookup_read
    def summary(self, completed_status, pending_states):
        completed = self.conn.execute(
            'SELECT COUNT(DISTINCT r.album_id) FROM album_description_requests r '
            'JOIN album_descriptions d ON d.request_id=r.id WHERE r.status=?', (completed_status,)).fetchone()[0]
        pending = self.conn.execute('SELECT 1 FROM album_description_requests WHERE status IN ('
            + ','.join('?' for _ in pending_states) + ') LIMIT 1', pending_states).fetchone() is not None
        return completed, pending

    @lookup_read
    def album_by_id(self, album_id):
        row = self.conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
        return self.canonical_album(row[0]) if row else None

    @lookup_read
    def album_by_browse_id(self, browse_id):
        return albums.lookup(self.conn, browse_id)

    @lookup_read
    def canonical_album(self, album_id):
        row = self.conn.execute('SELECT album_id FROM album_alias_reconciliations WHERE retired_id=?',
                                (album_id,)).fetchone()
        return row[0] if row else album_id

    @lookup_read
    def request_for(self, album_id, policy_version, status_order):
        album_id = self.canonical_album(album_id)
        ordering = 'CASE status ' + ' '.join('WHEN ? THEN ?' for _ in status_order) + ' ELSE ? END'
        parameters = [value for index, status in enumerate(status_order) for value in (status, index)]
        row = self.conn.execute('SELECT id FROM album_description_requests WHERE policy_version=? AND '
            'album_id IN (SELECT ? UNION ALL SELECT retired_id FROM album_alias_reconciliations WHERE album_id=?) '
            'ORDER BY ' + ordering + ',id LIMIT 1',
            (policy_version, album_id, album_id, *parameters, len(status_order))).fetchone()
        return row[0] if row else None

    @lookup_read
    def request(self, request_id):
        cursor = self.conn.execute('SELECT * FROM album_description_requests WHERE id=?', (request_id,))
        row = cursor.fetchone()
        return dict(zip((column[0] for column in cursor.description), row)) if row else None

    @lookup_read
    def successful_reference(self, album_id):
        album_id = self.canonical_album(album_id)
        row = self.conn.execute('SELECT d.filename,d.content_digest FROM album_description_requests r '
            'JOIN album_descriptions d ON d.request_id=r.id WHERE r.album_id IN '
            '(SELECT ? UNION ALL SELECT retired_id FROM album_alias_reconciliations WHERE album_id=?) '
            'ORDER BY d.created_at DESC,d.id DESC LIMIT 1', (album_id, album_id)).fetchone()
        return tuple(row) if row else None

    @lookup_read
    def legacy_request(self, identity):
        row = self.conn.execute('SELECT id FROM album_description_requests '
            "WHERE legacy_identity=? AND resolution_status='legacy_unresolved'", (identity,)).fetchone()
        return row[0] if row else None

    @lookup_read
    def usage(self, period):
        return self.conn.execute("SELECT COALESCE(sum(units),0) FROM provider_usage "
            "WHERE provider='deepseek' AND period=? AND usage_kind='requests'", (period,)).fetchone()[0]

    @lookup_read
    def access_rows(self, credential, now, sticky_status):
        return [{'status': state, 'retry_at': retry} for state, retry in self.conn.execute(
            "SELECT status,retry_at FROM album_description_access WHERE provider='deepseek' "
            "AND ((scope='credential' AND credential_key=?) OR scope='host') "
            'AND (status=? OR retry_at>?)', (credential, sticky_status, now))]

    @lookup_read
    def access_state(self, scope, credential):
        row = self.conn.execute("SELECT status,retry_at,updated_at FROM album_description_access "
            "WHERE provider='deepseek' AND scope=? AND credential_key=?", (scope, credential)).fetchone()
        return dict(zip(('status', 'retry_at', 'updated_at'), row)) if row else None

    @lookup_read
    def legacy_usage(self):
        row = self.conn.execute('SELECT lease_until,claim_token,send_authorized,status '
                                'FROM legacy_album_description_usage').fetchone()
        return dict(zip(('lease_until', 'claim_token', 'send_authorized', 'status'), row)) if row else None

    @lookup_read
    def active_request(self, states):
        cursor = self.conn.execute('SELECT id,status,lease_until FROM album_description_requests WHERE status IN ('
                                  + ','.join('?' for _ in states) + ') LIMIT 1', states)
        row = cursor.fetchone()
        return dict(zip(('id', 'status', 'lease_until'), row)) if row else None

    @lookup_read
    def counted_attempt(self, request_id, attempt):
        row = self.conn.execute('SELECT period,units FROM provider_usage WHERE request_id=? '
            "AND attempt_number=? AND usage_kind='requests'", (request_id, attempt)).fetchone()
        return tuple(row) if row else None

    @synchronized
    def expire_request(self, request_id, status, keep_token, now):
        self.conn.execute('UPDATE album_description_requests SET status=?,lease_until=0,'
            'claim_token=CASE WHEN ? THEN claim_token ELSE NULL END,updated_at=? WHERE id=?',
            (status, keep_token, now, request_id))

    @lookup_read
    def policy_digest(self, version):
        row = self.conn.execute('SELECT content_digest FROM recommendation_policies WHERE version=?',
                                (version,)).fetchone()
        return row[0] if row else None

    @synchronized
    def reserve(self, album_id, version, key, request_id, attempt, token, credential, lease_until, period, now,
                *, initial_status, claimed_status, policy_reference=None):
        if policy_reference is not None:
            filename, digest, size = policy_reference
            existing = self.policy_digest(version)
            if existing is not None and existing != digest:
                raise ValueError(f'Conflicting immutable recommendation policy: {version!r}')
            self.conn.execute('INSERT INTO recommendation_policies(version,configuration_filename,content_digest,'
                'byte_size,configuration_offset,created_at,base_version) VALUES (?,?,?,?,0,?,NULL) '
                'ON CONFLICT(version) DO NOTHING', (version, filename, digest, size, now))
            self.conn.execute('UPDATE recommendation_impression_policies SET policy_version=raw_version,'
                "resolution_reason=NULL WHERE resolution_reason='missing_policy' AND raw_version=?", (version,))
        if request_id is None:
            request_id = self.conn.execute('INSERT INTO album_description_requests('
                'album_id,request_key,policy_version,resolution_status,status,attempt_count,created_at,updated_at) '
                "VALUES (?,?,?,'resolved',?,0,?,?)", (album_id, key, version, initial_status, now, now)).lastrowid
        self.conn.execute('UPDATE album_description_requests SET status=?,claim_token=?,lease_until=?,'
            'attempt_count=?,credential_key=?,authorized_at=NULL,retry_at=0,updated_at=? WHERE id=?',
            (claimed_status, token, lease_until, attempt, credential, now, request_id))
        self._usage(request_id, attempt, period, 'requests', 1, now)
        return request_id

    def _usage(self, request_id, attempt, period, kind, units, now):
        self.conn.execute('INSERT INTO provider_usage('
            'request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
            "VALUES (?,?,?,'deepseek',?,?,?,?)",
            (request_id, attempt, f'request:{request_id}:{attempt}:{kind}', period, kind, units, now))

    @synchronized
    def mark_sent(self, request_id, token, lease_until, now, *, expected_status, status):
        return self.conn.execute('UPDATE album_description_requests SET status=?,authorized_at=?,'
            'lease_until=?,updated_at=? WHERE id=? AND claim_token=? AND status=?',
            (status, now, lease_until, now, request_id, token, expected_status)).rowcount == 1

    @synchronized
    def complete(self, request_id, attempt, expected_token, decision, reference, tokens, period, now, *, model=None):
        filename, digest, _size = reference
        if model is not None:
            self.conn.execute('INSERT INTO album_descriptions('
                'request_id,filename,format_version,content_digest,provider,model_version,created_at) '
                "VALUES (?,?,'album-description-1',?,'deepseek',?,?)", (request_id, filename, digest, model, now))
        for kind, units in tokens.items():
            self._usage(request_id, attempt, period, kind, units, now)
        self.conn.execute('UPDATE album_description_requests SET status=?,claim_token=?,lease_until=0,retry_at=?,'
            'updated_at=?,outcome_filename=?,outcome_digest=?,outcome_format=? WHERE id=? AND claim_token=?',
            (decision['status'], decision['token'], decision['retry_at'], now, filename, digest,
             'album-description-1', request_id, expected_token))
        access = decision['access']
        if access is not None:
            self.conn.execute('INSERT INTO album_description_access('
                'provider,scope,credential_key,status,retry_at,updated_at) '
                "VALUES ('deepseek',?,?,?,?,?) ON CONFLICT(provider,scope,credential_key) DO UPDATE SET "
                'status=excluded.status,retry_at=excluded.retry_at,updated_at=excluded.updated_at',
                (access['scope'], access['credential'], access['status'], access['retry_at'], access['updated_at']))
