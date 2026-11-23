"""Paid requests, results and usage in one SQLite transaction."""
from contextlib import contextmanager

from yt_playlist.core import albums, paid_description_access
from yt_playlist.repos.base import Repo, lookup_read, synchronized


REQUEST_COLUMNS = (
    'id', 'album_id', 'request_key', 'policy_version', 'resolution_status', 'legacy_identity',
    'status', 'claim_token', 'lease_until', 'attempt_count', 'credential_key', 'authorized_at',
    'retry_at', 'created_at', 'updated_at', 'outcome_json',
)


class AlbumDescriptionsRepo(Repo):
    @contextmanager
    def transaction(self):
        """Reserve the writer before making an admission or completion decision."""
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
            'SELECT COUNT(DISTINCT album_id) FROM album_description_requests WHERE status=?',
            (completed_status,)).fetchone()[0]
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
        row = self.conn.execute('SELECT COALESCE(canonical_album_id,id) FROM albums WHERE id=?',
                                (album_id,)).fetchone()
        return row[0] if row else album_id

    @lookup_read
    def request_for(self, album_id, policy_version, status_order):
        album_id = self.canonical_album(album_id)
        ordering = 'CASE status ' + ' '.join('WHEN ? THEN ?' for _ in status_order) + ' ELSE ? END'
        parameters = [value for index, status in enumerate(status_order) for value in (status, index)]
        row = self.conn.execute('SELECT id FROM album_description_requests WHERE policy_version=? AND '
            'album_id IN (SELECT ? UNION ALL SELECT id FROM albums WHERE canonical_album_id=?) '
            'ORDER BY ' + ordering + ',id LIMIT 1',
            (policy_version, album_id, album_id, *parameters, len(status_order))).fetchone()
        return row[0] if row else None

    @lookup_read
    def request(self, request_id):
        row = self.conn.execute(
            f'SELECT {", ".join(REQUEST_COLUMNS)} FROM album_description_requests WHERE id=? AND usage_id IS NULL',
            (request_id,)).fetchone()
        return dict(zip(REQUEST_COLUMNS, row, strict=True)) if row else None

    @lookup_read
    def successful_result(self, album_id):
        album_id = self.canonical_album(album_id)
        row = self.conn.execute("SELECT outcome_json FROM album_description_requests WHERE status='ready' AND album_id IN "
            '(SELECT ? UNION ALL SELECT id FROM albums WHERE canonical_album_id=?) '
            "AND json_extract(outcome_json,'$.status') IS NOT 'unavailable' "
            'ORDER BY result_created_at DESC,result_order DESC LIMIT 1', (album_id, album_id)).fetchone()
        return row[0] if row else None

    @lookup_read
    def legacy_request(self, identity):
        row = self.conn.execute('SELECT id FROM album_description_requests '
            "WHERE legacy_identity=? AND resolution_status='legacy_unresolved'", (identity,)).fetchone()
        return row[0] if row else None

    @lookup_read
    def usage(self, period):
        return self.conn.execute('SELECT COALESCE(sum(units),0) FROM album_description_requests '
            "WHERE usage_id IS NOT NULL AND provider='deepseek' AND period=? AND usage_kind='requests'",
            (period,)).fetchone()[0]

    @lookup_read
    def access_rows(self, credential, now, sticky_status):
        states = (paid_description_access.read(self.conn, 'credential', credential),
                  paid_description_access.read(self.conn, 'host', ''))
        return [{'status': row['status'], 'retry_at': row['retry_at']} for row in states
                if row and (row['status'] == sticky_status or row['retry_at'] > now)]

    @lookup_read
    def access_state(self, scope, credential):
        return paid_description_access.read(self.conn, scope, credential)

    @lookup_read
    def active_request(self, states):
        cursor = self.conn.execute('SELECT id,status,lease_until FROM album_description_requests WHERE status IN ('
                                  + ','.join('?' for _ in states) + ') LIMIT 1', states)
        row = cursor.fetchone()
        return dict(zip(('id', 'status', 'lease_until'), row)) if row else None

    @lookup_read
    def counted_attempt(self, request_id, attempt):
        row = self.conn.execute('SELECT period,units FROM album_description_requests WHERE request_id=? '
            "AND attempt_number=? AND usage_kind='requests'", (request_id, attempt)).fetchone()
        return tuple(row) if row else None

    @synchronized
    def expire_request(self, request_id, status, keep_token, now):
        self.conn.execute('UPDATE album_description_requests SET status=?,lease_until=0,'
            'claim_token=CASE WHEN ? THEN claim_token ELSE NULL END,updated_at=? WHERE id=? AND usage_id IS NULL',
            (status, keep_token, now, request_id))

    @synchronized
    def reserve(self, album_id, version, key, request_id, attempt, token, credential, lease_until, period, now,
                *, initial_status, claimed_status):
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
        self.conn.execute('INSERT INTO album_description_requests('
            'usage_id,request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
            "SELECT COALESCE(max(usage_id),0)+1,?,?,?,'deepseek',?,?,?,? FROM album_description_requests",
            (request_id, attempt, f'request:{request_id}:{attempt}:{kind}', period, kind, units, now))

    @synchronized
    def mark_sent(self, request_id, token, lease_until, now, *, expected_status, status):
        return self.conn.execute('UPDATE album_description_requests SET status=?,authorized_at=?,'
            'lease_until=?,updated_at=? WHERE id=? AND claim_token=? AND status=?',
            (status, now, lease_until, now, request_id, token, expected_status)).rowcount == 1

    @synchronized
    def complete(self, request_id, attempt, expected_token, decision, raw, tokens, period, now):
        for kind, units in tokens.items():
            self._usage(request_id, attempt, period, kind, units, now)
        result_at = now if decision['status'] == 'ready' else None
        self.conn.execute('UPDATE album_description_requests SET status=?,claim_token=?,lease_until=0,retry_at=?,'
            'updated_at=?,outcome_json=?,result_created_at=?,result_order=CASE WHEN ? IS NOT NULL '
            'THEN (SELECT COALESCE(MAX(result_order),0)+1 FROM album_description_requests) END '
            'WHERE id=? AND claim_token=?',
            (decision['status'], decision['token'], decision['retry_at'], now, raw, result_at, result_at,
             request_id, expected_token))
        access = decision['access']
        if access is not None:
            paid_description_access.write(self.conn, access)
