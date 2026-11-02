"""Separate paid outcomes, request admission, access waits and metered usage."""
import json
import math

from yt_playlist.core import album_description_files as files
from .runner import MigrationError

RESULT_PREFIX = 'album_description:'
CREDENTIAL_PREFIX = 'deepseek_credential:'
STATES = ('pending', 'claimed', 'sent', 'uncertain', 'ready', 'unknown',
          'invalid_response', 'auth', 'balance', 'rate_limit', 'unavailable', 'paused')
USAGE_KINDS = ('requests', 'prompt_tokens', 'completion_tokens', 'total_tokens')
LEGACY_KEYS = ('album_description_usage', 'deepseek_host')


def _finite(column, *, optional=False):
    condition = f"typeof({column}) IN ('real','integer') AND abs({column})<=1.7976931348623157e308"
    return f'CHECK({column} IS NULL OR ({condition}))' if optional else f'CHECK({condition})'


def statements():
    yield ('CREATE INDEX ix_album_reconciliation_canonical ON '
           'album_alias_reconciliations(album_id,retired_id)')
    yield ('CREATE TABLE album_description_requests ('
           'id INTEGER PRIMARY KEY AUTOINCREMENT,album_id INTEGER REFERENCES albums(id),'
           'request_key TEXT NOT NULL UNIQUE CHECK(length(request_key)>0),'
           'policy_version TEXT REFERENCES recommendation_policies(version),'
           "resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','legacy_unresolved')),"
           'legacy_identity TEXT UNIQUE,'
           'status TEXT NOT NULL CHECK(status IN (' + ','.join(repr(state) for state in STATES) + ')),'
           'claim_token TEXT UNIQUE,lease_until REAL NOT NULL DEFAULT 0 ' + _finite('lease_until') + ','
           'attempt_count INTEGER CHECK(attempt_count IS NULL OR '
           "(typeof(attempt_count)='integer' AND attempt_count>=0)),"
           'credential_key TEXT,authorized_at REAL ' + _finite('authorized_at', optional=True) + ','
           'retry_at REAL NOT NULL DEFAULT 0 ' + _finite('retry_at') + ','
           'created_at REAL NOT NULL ' + _finite('created_at') + ','
           'updated_at REAL NOT NULL ' + _finite('updated_at') + ','
           'outcome_filename TEXT,outcome_digest TEXT,outcome_format TEXT,'
           'CHECK((outcome_filename IS NULL AND outcome_digest IS NULL AND outcome_format IS NULL) OR '
           '(outcome_filename IS NOT NULL AND outcome_digest IS NOT NULL AND outcome_format IS NOT NULL '
           "AND length(outcome_filename)>0 AND outcome_format='album-description-1' "
           "AND length(outcome_digest)=64 AND outcome_digest NOT GLOB '*[^0-9a-f]*')),"
           "CHECK((resolution_status='resolved' AND album_id IS NOT NULL AND policy_version IS NOT NULL "
           "AND legacy_identity IS NULL AND attempt_count IS NOT NULL) OR "
           "(resolution_status='legacy_unresolved' AND album_id IS NULL AND policy_version IS NULL "
           'AND legacy_identity IS NOT NULL AND length(legacy_identity)>0 AND attempt_count IS NULL)),'
           "CHECK((status IN ('claimed','sent','uncertain') AND claim_token IS NOT NULL "
           "AND length(claim_token)>0 AND credential_key IS NOT NULL AND lease_until>=0) OR "
           "(status NOT IN ('claimed','sent','uncertain') AND claim_token IS NULL AND lease_until=0)),"
           "CHECK(status NOT IN ('sent','uncertain') OR authorized_at IS NOT NULL),"
           'UNIQUE(album_id,policy_version))')
    yield 'CREATE INDEX ix_album_description_album ON album_description_requests(album_id,policy_version,status)'
    yield ('CREATE INDEX ix_album_description_policy ON album_description_requests(policy_version,id) '
           'WHERE policy_version IS NOT NULL')
    yield ("CREATE INDEX ix_album_description_active ON album_description_requests(lease_until,id) "
           "WHERE status IN ('claimed','sent')")
    yield ("CREATE UNIQUE INDEX ix_album_description_single_active ON album_description_requests((1)) "
           "WHERE status IN ('claimed','sent')")
    yield ('CREATE TABLE album_descriptions (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'request_id INTEGER NOT NULL UNIQUE REFERENCES album_description_requests(id),'
           'filename TEXT NOT NULL CHECK(length(filename)>0),'
           "format_version TEXT NOT NULL CHECK(format_version='album-description-1'),"
           "content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),"
           'provider TEXT NOT NULL CHECK(provider=\'deepseek\'),model_version TEXT NOT NULL CHECK(length(model_version)>0),'
           'created_at REAL NOT NULL ' + _finite('created_at') + ')')
    yield ('CREATE TRIGGER album_descriptions_immutable BEFORE UPDATE ON album_descriptions WHEN '
           'NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id OR NEW.content_digest IS NOT OLD.content_digest '
           'OR NEW.format_version IS NOT OLD.format_version OR NEW.provider IS NOT OLD.provider '
           'OR NEW.model_version IS NOT OLD.model_version OR NEW.created_at IS NOT OLD.created_at '
           "BEGIN SELECT RAISE(ABORT,'Paid album description is immutable'); END")
    yield ('CREATE TRIGGER album_descriptions_retained BEFORE DELETE ON album_descriptions '
           "BEGIN SELECT RAISE(ABORT,'Paid album descriptions have no cache expiry'); END")
    yield ('CREATE TRIGGER album_description_owner_immutable BEFORE UPDATE ON album_description_requests WHEN '
           'NEW.id IS NOT OLD.id OR NEW.album_id IS NOT OLD.album_id OR NEW.request_key IS NOT OLD.request_key '
           'OR NEW.policy_version IS NOT OLD.policy_version OR NEW.legacy_identity IS NOT OLD.legacy_identity '
           'OR NEW.resolution_status IS NOT OLD.resolution_status OR NEW.created_at IS NOT OLD.created_at '
           "BEGIN SELECT RAISE(ABORT,'Album description request ownership is immutable'); END")
    yield ('CREATE TRIGGER album_description_request_retained BEFORE DELETE ON album_description_requests '
           "BEGIN SELECT RAISE(ABORT,'Paid request evidence has no cache expiry'); END")
    yield ('CREATE TRIGGER album_description_attempt_monotonic BEFORE UPDATE ON album_description_requests '
           'WHEN NEW.attempt_count<OLD.attempt_count '
           "BEGIN SELECT RAISE(ABORT,'Counted description attempts cannot be reset'); END")
    yield ('CREATE TRIGGER album_description_success_retained BEFORE UPDATE ON album_description_requests '
           "WHEN OLD.status='ready' AND (NEW.status IS NOT OLD.status OR NEW.outcome_digest IS NOT OLD.outcome_digest) "
           "BEGIN SELECT RAISE(ABORT,'Successful album description cannot be replaced'); END")
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER album_description_success_{event.lower()} BEFORE {event} ON album_description_requests '
               "WHEN NEW.status='ready' AND NOT EXISTS(SELECT 1 FROM album_descriptions d "
               'WHERE d.request_id=NEW.id AND d.content_digest=NEW.outcome_digest) '
               "BEGIN SELECT RAISE(ABORT,'Successful request requires its durable result'); END")
    yield ('CREATE TABLE legacy_album_description_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           "source_key TEXT NOT NULL UNIQUE CHECK(source_key='album_description_usage'),"
           "provider TEXT NOT NULL CHECK(provider='deepseek'),period INTEGER NOT NULL CHECK(typeof(period)='integer'),"
           "request_count INTEGER NOT NULL CHECK(typeof(request_count)='integer' AND request_count>=0),"
           'credential_key TEXT,status TEXT NOT NULL CHECK(status IN (' + ','.join(repr(state) for state in STATES) + ')),'
           'claim_token TEXT,lease_until REAL NOT NULL ' + _finite('lease_until') + ','
           "send_authorized INTEGER NOT NULL CHECK(typeof(send_authorized)='integer' AND send_authorized IN (0,1)),"
           'retry_at REAL NOT NULL ' + _finite('retry_at') + ','
           'built_at REAL NOT NULL ' + _finite('built_at') + ','
           'filename TEXT NOT NULL CHECK(length(filename)>0),'
           "content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'))")
    immutable = ('id', 'source_key', 'provider', 'period', 'request_count', 'credential_key',
                 'status', 'claim_token', 'lease_until', 'send_authorized', 'retry_at',
                 'built_at', 'content_digest')
    yield ('CREATE TRIGGER legacy_album_description_usage_immutable BEFORE UPDATE ON legacy_album_description_usage WHEN '
           + ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in immutable)
           + " BEGIN SELECT RAISE(ABORT,'Original paid usage evidence is immutable'); END")
    yield ('CREATE TABLE provider_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'request_id INTEGER REFERENCES album_description_requests(id),'
           'legacy_usage_id INTEGER REFERENCES legacy_album_description_usage(id),'
           "attempt_number INTEGER CHECK(attempt_number IS NULL OR (typeof(attempt_number)='integer' AND attempt_number>0)),"
           'reservation_key TEXT NOT NULL UNIQUE CHECK(length(reservation_key)>0),'
           "provider TEXT NOT NULL CHECK(provider='deepseek'),period INTEGER NOT NULL CHECK(typeof(period)='integer'),"
           'usage_kind TEXT NOT NULL CHECK(usage_kind IN (' + ','.join(repr(kind) for kind in USAGE_KINDS) + ')),'
           "units INTEGER NOT NULL CHECK(typeof(units)='integer' AND units>=0),"
           'created_at REAL NOT NULL ' + _finite('created_at') + ','
           'CHECK((request_id IS NOT NULL)+(legacy_usage_id IS NOT NULL)=1),'
           'CHECK(legacy_usage_id IS NULL OR attempt_number IS NULL),'
           'UNIQUE(request_id,attempt_number,usage_kind))')
    yield 'CREATE INDEX ix_provider_usage_period ON provider_usage(provider,period,usage_kind,units)'
    yield 'CREATE INDEX ix_provider_usage_request ON provider_usage(request_id,usage_kind)'
    yield ('CREATE UNIQUE INDEX ix_provider_usage_legacy_result ON provider_usage(request_id,usage_kind) '
           'WHERE request_id IS NOT NULL AND attempt_number IS NULL')
    yield ('CREATE UNIQUE INDEX ix_provider_usage_legacy_count ON provider_usage(legacy_usage_id,usage_kind) '
           'WHERE legacy_usage_id IS NOT NULL')
    yield ('CREATE TRIGGER provider_usage_attempt BEFORE INSERT ON provider_usage WHEN EXISTS('
           'SELECT 1 FROM album_description_requests r WHERE r.id=NEW.request_id AND '
           "((r.resolution_status='resolved' AND (NEW.attempt_number IS NULL OR NEW.attempt_number>r.attempt_count)) OR "
           "(r.resolution_status='legacy_unresolved' AND NEW.attempt_number IS NOT NULL))) "
           "BEGIN SELECT RAISE(ABORT,'Usage requires its actual request attempt'); END")
    for event in ('UPDATE', 'DELETE'):
        yield (f'CREATE TRIGGER provider_usage_{event.lower()} BEFORE {event} ON provider_usage '
               "BEGIN SELECT RAISE(ABORT,'Metered provider usage is immutable'); END")
    yield ('CREATE TABLE album_description_access (provider TEXT NOT NULL CHECK(provider=\'deepseek\'),'
           "scope TEXT NOT NULL CHECK(scope IN ('host','credential')),credential_key TEXT NOT NULL,"
           "status TEXT NOT NULL CHECK(status IN ('auth','balance','rate_limit','unavailable')),"
           'retry_at REAL NOT NULL ' + _finite('retry_at') + ','
           'updated_at REAL NOT NULL ' + _finite('updated_at') + ','
           'legacy_usage_id INTEGER REFERENCES legacy_album_description_usage(id),'
           'evidence_filename TEXT,evidence_digest TEXT,'
           'CHECK((evidence_filename IS NULL AND evidence_digest IS NULL) OR '
           '(evidence_filename IS NOT NULL AND length(evidence_filename)>0 AND evidence_digest IS NOT NULL '
           "AND length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*')),"
           "CHECK((scope='host' AND credential_key='' AND status='unavailable') OR "
           "(scope='credential' AND length(credential_key)>0 AND status IN ('auth','balance','rate_limit'))),"
           'PRIMARY KEY(provider,scope,credential_key)) WITHOUT ROWID')


def _time(value, field):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'Invalid {field}')
    return value


def _integer(value, field, *, nonnegative=True):
    if type(value) is not int or nonnegative and value < 0:
        raise ValueError(f'Invalid {field}')
    return value


def _text(value, field, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f'Invalid {field}')
    return value


def _original(surface, raw, built):
    files.document(raw)
    value = json.loads(raw)
    _time(built, 'capture time')
    if surface.startswith(RESULT_PREFIX):
        _text(surface[len(RESULT_PREFIX):], 'legacy result identity')
        if value.get('status') not in STATES or value['status'] in ('pending', 'claimed', 'sent', 'uncertain'):
            raise ValueError('Invalid legacy result status')
        _time(value.get('at', built), 'result time')
        _time(value.get('retry_at', 0), 'retry time')
        if value.get('description') is not None and not isinstance(value['description'], str):
            raise ValueError('Invalid paid text')
        if (value.get('status') == 'ready') != bool(value.get('description')):
            raise ValueError('Inconsistent successful paid result')
        if value.get('description'):
            _text(value.get('model'), 'paid result model')
        usage = value.get('usage', {})
        if not isinstance(usage, dict) or set(usage) - set(USAGE_KINDS[1:]):
            raise ValueError('Unrecognized token accounting')
        for field, units in usage.items():
            _integer(units, field)
    elif surface == 'album_description_usage':
        _integer(value.get('day'), 'usage period', nonnegative=False)
        _integer(value.get('requests'), 'request count')
        _text(value.get('credential'), 'credential identity', optional=True)
        _text(value.get('token'), 'legacy claim token', optional=True)
        if value.get('status') not in (*STATES, 'reserved'):
            raise ValueError('Invalid usage status')
        if value.get('status') in ('auth', 'balance', 'rate_limit') and not value.get('credential'):
            raise ValueError('Legacy provider failure has no credential identity')
        _time(value.get('lease_until', 0), 'lease deadline')
        _time(value.get('retry_at', 0), 'retry time')
        if type(value.get('send_authorized', False)) is not bool:
            raise ValueError('Invalid send authorization')
    else:
        scope = 'host' if surface == 'deepseek_host' else 'credential'
        if scope == 'credential':
            _text(surface[len(CREDENTIAL_PREFIX):], 'credential identity')
        if value.get('status') not in (('unavailable',) if scope == 'host' else ('auth', 'balance', 'rate_limit')):
            raise ValueError('Invalid access wait')
        _time(value.get('retry_at', 0), 'retry time')
    return value


def _rows(conn):
    return conn.execute("SELECT surface,payload,built_at FROM rec_proposals WHERE "
        "surface GLOB 'album_description:*' OR surface IN ('album_description_usage','deepseek_host') "
        "OR surface GLOB 'deepseek_credential:*' ORDER BY surface")


def apply(conn, context):
    for surface, raw, built in _rows(conn):
        try:
            _original(surface, raw, built)
        except (ValueError, TypeError) as exc:
            raise MigrationError(f'Invalid paid description evidence {surface!r}: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    copy_rows(conn, _rows(conn))
    conn.execute("DELETE FROM rec_proposals WHERE surface GLOB 'album_description:*' "
                 "OR surface IN ('album_description_usage','deepseek_host') OR surface GLOB 'deepseek_credential:*'")


def copy_rows(conn, rows, *, access_overlay=True):
    """Copy a bounded paid-outcome batch into existing owners, without DDL."""
    root = files.directory(conn)
    for surface, raw, built in rows:
        _original(surface, raw, built)
        value = json.loads(raw)
        if surface.startswith(RESULT_PREFIX):
            filename, digest, _size = files.publish(root, raw)
            status = value['status']
            at = value.get('at', built)
            request = conn.execute('INSERT INTO album_description_requests('
                'request_key,resolution_status,legacy_identity,status,retry_at,created_at,updated_at,'
                'outcome_filename,outcome_digest,outcome_format) '
                "VALUES (?,'legacy_unresolved',?,'pending',?,?,?,?,?,?)",
                ('legacy:' + surface, surface[len(RESULT_PREFIX):], value.get('retry_at', 0), at,
                 built, filename, digest, files.FORMAT)).lastrowid
            if value.get('description'):
                conn.execute('INSERT INTO album_descriptions('
                    'request_id,filename,format_version,content_digest,provider,model_version,created_at) '
                    "VALUES (?,?,?,?,'deepseek',?,?)", (request, filename, files.FORMAT, digest, value['model'], at))
            conn.execute('UPDATE album_description_requests SET status=? WHERE id=?', (status, request))
            for kind, units in value.get('usage', {}).items():
                conn.execute('INSERT INTO provider_usage('
                    'request_id,reservation_key,provider,period,usage_kind,units,created_at) '
                    "VALUES (?,?,'deepseek',?,?,?,?)", (request, f'legacy-result:{request}:{kind}', int(at // 86400), kind, units, at))
        elif surface == 'album_description_usage':
            filename, digest, _size = files.publish(root, raw)
            usage_id = conn.execute('INSERT INTO legacy_album_description_usage('
                'source_key,provider,period,request_count,credential_key,status,claim_token,lease_until,'
                'send_authorized,retry_at,built_at,filename,content_digest) '
                "VALUES (?,'deepseek',?,?,?,?,?,?,?,?,?,?,?)", (surface, value['day'], value['requests'],
                value.get('credential'), 'claimed' if value['status'] == 'reserved' else value['status'],
                value.get('token'), value.get('lease_until', 0), int(value.get('send_authorized', False)),
                value.get('retry_at', 0), built, filename, digest)).lastrowid
            conn.execute('INSERT INTO provider_usage('
                'legacy_usage_id,reservation_key,provider,period,usage_kind,units,created_at) '
                "VALUES (?,?,'deepseek',?,'requests',?,?)",
                (usage_id, 'legacy:' + surface, value['day'], value['requests'], built))
        else:
            filename, digest, _size = files.publish(root, raw)
            conn.execute('INSERT INTO album_description_access('
                'provider,scope,credential_key,status,retry_at,updated_at,evidence_filename,evidence_digest) '
                'VALUES (?,?,?,?,?,?,?,?)',
                ('deepseek', 'host' if surface == 'deepseek_host' else 'credential',
                 '' if surface == 'deepseek_host' else surface[len(CREDENTIAL_PREFIX):],
                 value['status'], value.get('retry_at', 0), built, filename, digest))
    if access_overlay:
        copy_access_waits(conn)


def copy_access_waits(conn):
    """Apply global usage waits after the explicit access owners are copied."""
    for usage_id, credential, state, retry_at, built in conn.execute(
            'SELECT id,credential_key,status,retry_at,built_at FROM legacy_album_description_usage '
            "WHERE status IN ('auth','balance','rate_limit','unavailable')"):
        conn.execute('INSERT INTO album_description_access('
            'provider,scope,credential_key,status,retry_at,updated_at,legacy_usage_id) VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(provider,scope,credential_key) DO UPDATE SET '
            "status=CASE WHEN status='auth' OR excluded.status='auth' THEN 'auth' "
            'WHEN excluded.retry_at>retry_at THEN excluded.status ELSE status END,'
            'retry_at=MAX(retry_at,excluded.retry_at),updated_at=MAX(updated_at,excluded.updated_at),'
            'legacy_usage_id=excluded.legacy_usage_id', ('deepseek', 'host' if state == 'unavailable' else 'credential',
            '' if state == 'unavailable' else credential, state, retry_at, built, usage_id))


def validate(conn):
    from .current import _tokens
    for sql in statements():
        words = sql.split()
        name = words[3] if words[1] == 'UNIQUE' else words[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible paid description structure: {name}')
    if _rows(conn).fetchone():
        raise MigrationError('Superseded paid description keys remain in rec_proposals')
