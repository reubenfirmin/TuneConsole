"""Transactionally fenced paid requests; callers perform transport outside SQLite."""
from contextlib import contextmanager
import hashlib
import json
import math
import uuid

from . import album_description_files as files, policy_documents
from .evidence_ledger import canonical

LEASE_SECONDS = 60
FAILURES = ('auth', 'balance', 'rate_limit', 'unavailable', 'invalid_response', 'paused')
TOKEN_KINDS = ('prompt_tokens', 'completion_tokens', 'total_tokens')


def _time(now):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('Paid request time must be finite')


@contextmanager
def _write(conn):
    if conn.in_transaction:
        raise ValueError('Paid request admission requires its own transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _row(conn, request_id):
    cursor = conn.execute('SELECT * FROM album_description_requests WHERE id=?', (request_id,))
    value = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), value)) if value else None


def result(conn, request_id):
    row = _row(conn, request_id)
    if not row or row['outcome_filename'] is None:
        return None
    return json.loads(files.read(row['outcome_filename'], row['outcome_digest']))


def canonical_album(conn, album_id):
    row = conn.execute('SELECT album_id FROM album_alias_reconciliations WHERE retired_id=?', (album_id,)).fetchone()
    return row[0] if row else album_id


def request_for(conn, album_id, policy_version):
    """Verified aliases share admission; original request ownership stays intact."""
    album_id = canonical_album(conn, album_id)
    row = conn.execute('SELECT id FROM album_description_requests WHERE policy_version=? AND '
        'album_id IN (SELECT ? UNION ALL SELECT retired_id FROM album_alias_reconciliations WHERE album_id=?) '
        "ORDER BY CASE status WHEN 'ready' THEN 0 WHEN 'uncertain' THEN 1 WHEN 'sent' THEN 2 "
        "WHEN 'claimed' THEN 3 ELSE 4 END,id LIMIT 1", (policy_version, album_id, album_id)).fetchone()
    return row[0] if row else None


def usage(conn, now):
    _time(now)
    return conn.execute("SELECT COALESCE(sum(units),0) FROM provider_usage "
                        "WHERE provider='deepseek' AND period=? AND usage_kind='requests'",
                        (int(now // 86400),)).fetchone()[0]


def availability(conn, credential, now):
    _time(now)
    rows = conn.execute("SELECT status,retry_at FROM album_description_access WHERE provider='deepseek' "
                        "AND ((scope='credential' AND credential_key=?) OR scope='host') "
                        "AND (status='auth' OR retry_at>?)", (credential, now))
    return max(({'status': state, 'retry_at': retry} for state, retry in rows),
               key=lambda row: (row['status'] == 'auth', row['retry_at']), default={})


def claim(conn, album_id, policy_version, credential, daily_limit, now, *, configuration=None):
    """Count before transport; expired sent work remains uncertain across restart."""
    _time(now)
    if (type(album_id) is not int or type(daily_limit) is not int or not 0 <= daily_limit <= 100
            or not isinstance(policy_version, str) or not policy_version
            or not isinstance(credential, str) or not credential):
        raise ValueError('Invalid paid album request admission')
    with _write(conn):
        album_id = canonical_album(conn, album_id)
        existing = request_for(conn, album_id, policy_version)
        row = _row(conn, existing) if existing is not None else None
        if row and row['status'] == 'ready':
            return {'status': 'cached', 'record': result(conn, row['id'])}
        # A pre-schema lease contains no album identity. Preserve its conservative
        # admission effect without assigning it to whichever album asks next.
        legacy = conn.execute('SELECT lease_until,claim_token,send_authorized,status '
                              'FROM legacy_album_description_usage').fetchone()
        if legacy and legacy[0] > now:
            return {'status': 'busy', 'retry_at': legacy[0]}
        if legacy and legacy[1] and legacy[2] and legacy[3] in ('pending', 'claimed', 'sent', 'uncertain'):
            return {'status': 'uncertain'}
        active = conn.execute("SELECT id,status,lease_until FROM album_description_requests "
                              "WHERE status IN ('claimed','sent') LIMIT 1").fetchone()
        if active:
            if active[2] > now:
                return {'status': 'busy', 'retry_at': active[2]}
            if active[1] == 'sent':
                conn.execute("UPDATE album_description_requests SET status='uncertain',lease_until=0,updated_at=? "
                             'WHERE id=?', (now, active[0]))
            else:
                conn.execute("UPDATE album_description_requests SET status='paused',claim_token=NULL,lease_until=0,"
                             'updated_at=? WHERE id=?', (now, active[0]))
            if row and row['id'] == active[0]:
                row = _row(conn, row['id'])
        if row and row['status'] == 'uncertain':
            return {'status': 'uncertain'}
        if row and row['status'] in ('unknown', 'invalid_response') and row['retry_at'] > now:
            return {'status': 'cached', 'record': result(conn, row['id'])}
        wait = availability(conn, credential, now)
        count = usage(conn, now)
        if count >= daily_limit:
            return {'status': 'daily_limit', 'retry_at': (int(now // 86400) + 1) * 86400}
        if wait:
            return wait
        if configuration is not None:
            # The policy and its first paid owner become visible together. A
            # separate policy commit would leave a pruning race before admission.
            policy_documents.install(conn, [(policy_version, canonical(configuration).decode(), now)])
        if row is None:
            key = hashlib.sha256(canonical(['album-description-1', album_id, policy_version])).hexdigest()
            request_id = conn.execute('INSERT INTO album_description_requests('
                'album_id,request_key,policy_version,resolution_status,status,attempt_count,created_at,updated_at) '
                "VALUES (?,?,?,'resolved','pending',0,?,?)", (album_id, key, policy_version, now, now)).lastrowid
            row = _row(conn, request_id)
        attempt, token = row['attempt_count'] + 1, uuid.uuid4().hex
        conn.execute("UPDATE album_description_requests SET status='claimed',claim_token=?,lease_until=?,"
                     'attempt_count=?,credential_key=?,authorized_at=NULL,retry_at=0,updated_at=? WHERE id=?',
                     (token, now + LEASE_SECONDS, attempt, credential, now, row['id']))
        conn.execute('INSERT INTO provider_usage('
            'request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
            "VALUES (?,?,?,'deepseek',?,'requests',1,?)",
            (row['id'], attempt, f'request:{row["id"]}:{attempt}:requests', int(now // 86400), now))
        return {'status': 'reserved', 'request_id': row['id'], 'token': token, 'attempt': attempt}


def authorize(conn, request_id, token, credential, daily_limit, now):
    """One current-day reservation authorizes one bounded transport attempt."""
    _time(now)
    if type(daily_limit) is not int or not 0 <= daily_limit <= 100:
        raise ValueError('Invalid paid request daily limit')
    with _write(conn):
        row = _row(conn, request_id)
        if (not row or row['status'] != 'claimed' or row['claim_token'] != token
                or row['credential_key'] != credential or row['lease_until'] <= now
                or availability(conn, credential, now) or usage(conn, now) > daily_limit):
            return False
        counted = conn.execute('SELECT period,units FROM provider_usage WHERE request_id=? '
                               "AND attempt_number=? AND usage_kind='requests'",
                               (request_id, row['attempt_count'])).fetchone()
        if not counted or tuple(counted) != (int(now // 86400), 1):
            return False
        conn.execute("UPDATE album_description_requests SET status='sent',authorized_at=?,lease_until=?,updated_at=? "
                     "WHERE id=? AND claim_token=? AND status='claimed'",
                     (now, now + LEASE_SECONDS, now, request_id, token))
        return True


def _record(record):
    if not isinstance(record, dict) or record.get('status') not in (*FAILURES, 'ready', 'unknown'):
        raise ValueError('Invalid paid request completion')
    _time(record.get('retry_at', 0))
    if 'at' in record:
        _time(record['at'])
    text = record.get('description')
    if text is not None and not isinstance(text, str):
        raise ValueError('Invalid paid description text')
    if (record['status'] == 'ready') != bool(text):
        raise ValueError('Paid completion state does not match its description')
    if text and (not isinstance(record.get('model'), str) or not record['model']):
        raise ValueError('Paid description requires its exact model')
    tokens = record.get('usage', {})
    if (not isinstance(tokens, dict) or set(tokens) - set(TOKEN_KINDS)
            or any(type(units) is not int or units < 0 for units in tokens.values())):
        raise ValueError('Invalid metered token usage')
    raw = canonical(record).decode()
    files.document(raw)
    return raw, tokens


def finish(conn, request_id, token, record, now, *, memory_key=None):
    """Publish only this claim's outcome. A paid success survives later failures."""
    _time(now)
    raw, tokens = _record(record)
    with _write(conn):
        row = _row(conn, request_id)
        if (not row or row['claim_token'] != token or row['status'] not in ('claimed', 'sent', 'uncertain')):
            return False
        if row['status'] == 'claimed' and (tokens or record['status'] in ('ready', 'unknown')):
            raise ValueError('Paid output requires an authorized transport attempt')
        filename, digest, _size = files.publish(files.directory(conn, memory_key), raw)
        if record['status'] == 'ready':
            conn.execute('INSERT INTO album_descriptions('
                'request_id,filename,format_version,content_digest,provider,model_version,created_at) '
                "VALUES (?,?,?,?,'deepseek',?,?)", (request_id, filename, files.FORMAT, digest, record['model'], now))
        for kind, units in tokens.items():
            conn.execute('INSERT INTO provider_usage('
                'request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
                "VALUES (?,?,?,'deepseek',?,?,?,?)", (request_id, row['attempt_count'],
                f'request:{request_id}:{row["attempt_count"]}:{kind}', int(now // 86400), kind, units, now))
        state = ('uncertain' if row['status'] in ('sent', 'uncertain') and record['status'] == 'unavailable'
                 else record['status'])
        conn.execute('UPDATE album_description_requests SET status=?,claim_token=?,lease_until=0,retry_at=?,updated_at=?,'
                     'outcome_filename=?,outcome_digest=?,outcome_format=? WHERE id=? AND claim_token=?',
                     (state, token if state == 'uncertain' else None, record.get('retry_at', 0), now,
                      filename, digest, files.FORMAT, request_id, token))
        if record['status'] in ('auth', 'balance', 'rate_limit', 'unavailable'):
            scope = 'host' if record['status'] == 'unavailable' else 'credential'
            conn.execute('INSERT INTO album_description_access('
                'provider,scope,credential_key,status,retry_at,updated_at) '
                "VALUES ('deepseek',?,?,?,?,?) ON CONFLICT(provider,scope,credential_key) DO UPDATE SET "
                "status=CASE WHEN status='auth' OR excluded.status='auth' THEN 'auth' "
                'WHEN excluded.retry_at>retry_at THEN excluded.status ELSE status END,'
                'retry_at=MAX(retry_at,excluded.retry_at),updated_at=MAX(updated_at,excluded.updated_at)',
                (scope, '' if scope == 'host' else row['credential_key'], record['status'], record.get('retry_at', 0), now))
        return True
