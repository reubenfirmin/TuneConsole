"""Admission and outcome rules for paid album descriptions."""
import hashlib
import math

from yt_playlist.core.evidence_ledger import canonical

LEASE_SECONDS = 60
FAILURES = ('auth', 'balance', 'rate_limit', 'unavailable', 'invalid_response', 'paused')
TOKEN_KINDS = ('prompt_tokens', 'completion_tokens', 'total_tokens')
REQUEST_ORDER = ('ready', 'uncertain', 'sent', 'claimed')
ACTIVE_STATES = ('claimed', 'sent')
FINISHABLE_STATES = ('claimed', 'sent', 'uncertain')
PENDING_STATES = ('pending', 'claimed', 'sent')


def validate_time(now):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('Paid request time must be finite')


def validate_limit(daily_limit):
    if type(daily_limit) is not int or not 0 <= daily_limit <= 100:
        raise ValueError('Invalid paid request daily limit')


def validate_admission(album_id, version, credential, daily_limit, now):
    validate_time(now)
    validate_limit(daily_limit)
    if (type(album_id) is not int or not isinstance(version, str) or not version
            or not isinstance(credential, str) or not credential):
        raise ValueError('Invalid paid album request admission')


def policy_version(configuration):
    if not isinstance(configuration, dict):
        raise ValueError('Paid description policy must be an object')
    return hashlib.sha256(canonical(['album-description-policy-1', configuration])).hexdigest()


def request_key(album_id, version):
    return hashlib.sha256(canonical(['album-description-1', album_id, version])).hexdigest()


def period(now):
    validate_time(now)
    return int(now // 86400)


def availability(rows):
    return max(rows, key=lambda row: (row['status'] == 'auth', row['retry_at']), default={})


def legacy_block(legacy, now):
    if legacy and legacy['lease_until'] > now:
        return {'status': 'busy', 'retry_at': legacy['lease_until']}
    if (legacy and legacy['claim_token'] and legacy['send_authorized']
            and legacy['status'] in (*PENDING_STATES, 'uncertain')):
        return {'status': 'uncertain'}
    return None


def expired_state(row):
    return 'uncertain' if row['status'] == 'sent' else 'paused'


def admission(row, count, wait, daily_limit, now):
    if row and row['status'] == 'uncertain':
        return {'status': 'uncertain'}
    if row and row['status'] in ('unknown', 'invalid_response') and row['retry_at'] > now:
        return {'status': 'cached', 'request_id': row['id']}
    if count >= daily_limit:
        return {'status': 'daily_limit', 'retry_at': (period(now) + 1) * 86400}
    return wait or None


def can_authorize(row, token, credential, now, wait, count, daily_limit, counted):
    return bool(row and row['status'] == 'claimed' and row['claim_token'] == token
                and row['credential_key'] == credential and row['lease_until'] > now
                and not wait and count <= daily_limit and counted == (period(now), 1))


def record(record):
    if not isinstance(record, dict) or record.get('status') not in (*FAILURES, 'ready', 'unknown'):
        raise ValueError('Invalid paid request completion')
    validate_time(record.get('retry_at', 0))
    if 'at' in record:
        validate_time(record['at'])
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
    return canonical(record).decode(), tokens


def can_finish(row, token, record, tokens):
    if not row or row['claim_token'] != token or row['status'] not in FINISHABLE_STATES:
        return False
    if row['status'] == 'claimed' and (tokens or record['status'] in ('ready', 'unknown')):
        raise ValueError('Paid output requires an authorized transport attempt')
    return True


def completion(row, token, record):
    status = ('uncertain' if row['status'] in ('sent', 'uncertain') and record['status'] == 'unavailable'
              else record['status'])
    access = None
    if record['status'] in ('auth', 'balance', 'rate_limit', 'unavailable'):
        scope = 'host' if record['status'] == 'unavailable' else 'credential'
        access = {'scope': scope, 'credential': '' if scope == 'host' else row['credential_key'],
                  'status': record['status'], 'retry_at': record.get('retry_at', 0)}
    return {'status': status, 'token': token if status == 'uncertain' else None,
            'retry_at': record.get('retry_at', 0), 'access': access}


def merge_access(access, previous, now):
    result = {**access, 'updated_at': now}
    if previous:
        result['status'] = ('auth' if 'auth' in (previous['status'], access['status']) else
                            access['status'] if access['retry_at'] > previous['retry_at'] else previous['status'])
        result['retry_at'] = max(previous['retry_at'], access['retry_at'])
        result['updated_at'] = max(previous['updated_at'], now)
    return result
