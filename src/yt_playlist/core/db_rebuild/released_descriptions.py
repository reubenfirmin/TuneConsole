"""Preserve paid results, usage and access waits from released source messages."""
import json
import math

from .. import album_description_files as files


RESULT_PREFIX = 'album_description:'
CREDENTIAL_PREFIX = 'deepseek_credential:'
STATES = ('pending', 'claimed', 'sent', 'uncertain', 'ready', 'unknown',
          'invalid_response', 'auth', 'balance', 'rate_limit', 'unavailable', 'paused')
USAGE_KINDS = ('requests', 'prompt_tokens', 'completion_tokens', 'total_tokens')
LEGACY_KEYS = ('album_description_usage', 'deepseek_host')


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


def copy_rows(conn, rows, *, access_overlay=True):
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
                "VALUES (?,?,'deepseek',?,'requests',?,?)", (usage_id, 'legacy:' + surface, value['day'], value['requests'], built))
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
