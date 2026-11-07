"""Retry schedules, provider coverage, and configuration refresh decisions."""
import json
from sqlite3 import IntegrityError

from .activity import WorkBudgetExceeded
from .metadata_contract import ALTERNATIVE_SECONDS
from .schema_contract import MigrationError

MAX_STATE_BYTES = 8 * 1024 * 1024


def attempt_status(fields, retry):
    if type(retry) is not bool:
        raise ValueError('Provider retry flag must be a boolean')
    return 'retry' if retry else 'found' if fields else 'no_match'


def attempt_replayed(previous, status):
    if previous is None:
        return False
    if previous['status'] != status:
        raise IntegrityError('A provider attempt replay cannot change its outcome')
    return True


def stale_attempt(previous, prior, captured, run_id, now):
    return previous is not None and (now <= previous['at'] or captured.at <= previous['at']
                                    or bool(run_id) and prior.get('run_id') == run_id)


def attempt_state(previous, cycle, fields, *, now, retry, retry_after, error, prior=None, runtime=False):
    same_cycle = previous is not None and previous['cycle'] == cycle
    failures = ((previous['failures'] if same_cycle else 0) + 1) if retry else 0
    deadline = max(now + min(3600, 30 * 2 ** min(failures - 1, 7)), retry_after) if retry else 0
    retained = {**(prior or {} if same_cycle else {}), **fields}
    if runtime:
        if 'year' in fields and 'mb_year' not in fields:
            retained.pop('mb_year', None)
        elif 'mb_year' in fields and 'year' not in fields:
            retained.pop('year', None)
    return {'at': now, 'status': attempt_status(fields, retry), 'fields': retained, 'failures': failures,
            'retry_at': deadline, 'error': '' if runtime else error, 'reset_fields': not same_cycle}


def attempt_document(subject, provider, result, run_id, error):
    return {'entry_id': subject['entry_id'], 'recording_id': subject['recording_id'],
            'input_video_id': subject['video_id'], 'provider': provider,
            'fields': result['fields'], 'run_id': run_id, 'at': result['at'],
            'expires_at': result['at'] + ALTERNATIVE_SECONDS, 'error': error}


def alternative_available(row, now):
    return row['provider_id'] is not None and row['state']['at'] > now - ALTERNATIVE_SECONDS


def field_deliveries(rows):
    references = {row['delivery_id']: row['reference'] for row in rows}
    sizes = [reference[2] for reference in references.values()]
    if any(size is None for size in sizes) or sum(sizes) > MAX_STATE_BYTES:
        raise WorkBudgetExceeded('Provider state exceeds its delivery read budget')
    return references


def prepared_fields(body, fields):
    values = json.loads(body['row']['fields_json'])
    if not isinstance(values, dict) or list(values) != fields:
        raise MigrationError('Metadata operational fields differ from their retained delivery')
    return values


def referenced_fields(rows, bodies):
    result = {}
    for row in rows:
        body = bodies[row['delivery_id']]
        if row['field'] not in body:
            raise ValueError('Provider field reference differs from its delivery')
        result[row['field']] = body[row['field']]
    return result


def observation_page_bounds(after_id, limit):
    if type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError('Observation page size must be between 1 and 512')
    if type(after_id) is not int or after_id < 0:
        raise ValueError('Invalid observation cursor')


def runtime_observations(row, data, after_id):
    for position, (field, value) in enumerate(sorted(data['observations'].items()), 1):
        cursor = row['cursor'] * 64 + position
        if cursor > after_id:
            yield {'id': cursor, 'delivery_id': data.get('run_id'), 'provider': row['provider'],
                'entry_id': data.get('entry_id'), 'recording_id': data.get('recording_id'),
                'observed_at': data.get('at'), 'field': field,
                'value': value['value'], 'provenance': value['provenance']}


def native_observation(row, payload):
    provenance = {name: value for name, value in payload.items() if name not in ('field', 'value')}
    provenance.setdefault('delivery_id', row['delivery_key'])
    return {**{name: row[name] for name in ('id', 'delivery_id', 'provider', 'entry_id', 'recording_id', 'observed_at')},
            'field': payload['field'], 'value': payload['value'], 'provenance': provenance}


def integration_retry(provider, previous, *, now, retry_after, error):
    failures = (previous['failures'] if previous else 0) + 1
    deadline = max(retry_after, now + min(3600, 30 * 2 ** min(failures - 1, 7)),
                   previous['retry_at'] if previous else 0)
    return dict(provider=provider, failures=failures, retry_at=deadline, error=error)


def artwork_retry(previous, outcome, now):
    failures = (previous['failures'] if previous else 0) + 1 if outcome.status == 'retry' else 0
    retry_at = now + max(outcome.retry_in, min(900, 60 * 2 ** min(failures - 1, 4))) if failures else 0
    return dict(status=outcome.status, failures=failures, retry_at=retry_at, error=outcome.error)


def coverage_status(configured, state, pending):
    if configured != 'available':
        return configured
    return state or ('pending' if pending else 'unknown')


def log_scope(field):
    return ('release' if field in ('year', 'label', 'mb_release_ids', 'cover_url') else
            'item' if field in ('popularity', 'gain', 'loudness', 'dynamic_complexity') else 'recording')


def identifiers(field, value, *, strict):
    if not value or field not in ('mb_recording_id', 'isrc'):
        return None, []
    supplied = value if isinstance(value, (list, tuple)) else [value]
    if strict and (len(supplied) > 1024 or any(not isinstance(identifier, str) or
            not 0 < len(identifier.encode()) <= 4096 for identifier in supplied)):
        raise ValueError('Identifier evidence requires bounded nonempty text')
    return 'musicbrainz' if field == 'mb_recording_id' else 'isrc', [str(identifier) for identifier in supplied]


def configuration_changes(configuration, inputs):
    changes = []
    for provider, current in configuration.items():
        previous = inputs['configuration'].get(provider)
        changed = previous is not None and (previous['fingerprint'] != current['fingerprint'] or
                  previous['status'] != 'available' and current['status'] == 'available')
        replace = previous is None or any(previous[key] != current[key] for key in ('fingerprint', 'status'))
        clear = current['status'] != 'available' and inputs['frontiers'].get(provider) is not None
        restart = current['status'] == 'available' and changed
        if replace or clear or restart:
            changes.append(dict(provider=provider, current=current, replace=replace, clear=clear, restart=restart))
    return changes


def refresh_page(rows, cutoff, *, runtime):
    stale, jobs = [], []
    for row in rows:
        if runtime:
            newer = row['at'] is not None and row['at'] * 1_000_000 > cutoff
            completed = newer and row['status'] != 'retry'
        else:
            newer = row['last_attempt'] > cutoff
            completed = row['completed_attempt'] > cutoff
        if row['at'] is not None and not newer:
            stale.append(row['id'])
        if row['first_enriched_at'] is not None and not completed:
            jobs.append(row['id'])
    return stale, jobs
