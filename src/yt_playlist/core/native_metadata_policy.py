"""Validate native evidence and decide source revisions without accessing storage."""
import hashlib
import json
import math

from .evidence_ledger import canonical

FORMAT = 'native-metadata-observations-1'
FIELDS = frozenset(('tags', 'bpm', 'energy', 'danceability', 'mood_party', 'mood_aggressive',
                    'music_key', 'music_scale', 'mood_happy', 'mood_sad', 'mood_relaxed',
                    'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity'))
MAX_OBSERVATIONS = 64
MODEL_SCOPES = frozenset(('recording', 'artist', 'release'))


def profile_value(payload):
    provenance = {key: value for key, value in payload.items() if key not in ('field', 'value')}
    provenance.pop('delivery_id', None)
    match = dict(provenance.get('match_evidence') or {})
    for key in ('input_title', 'input_artist', 'returned_title', 'returned_artist', 'recording_mbid', 'search_score'):
        match.pop(key, None)
    if 'match_evidence' in provenance:
        provenance['match_evidence'] = match
    return {'value': payload['value'], 'provenance': provenance}


def result_hash(values):
    normalized = {payload['field']: profile_value(payload) for payload in values}
    return hashlib.sha256(canonical(normalized)).digest()


def prepare(provider, observations, run_id, now, *, native):
    if (not isinstance(provider, str) or not provider.strip()
            or not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError('Observation provider and delivery ID are required')
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('Observation time must be finite')
    grouped, prepared = {}, []
    for observation in observations:
        if native and len(prepared) == MAX_OBSERVATIONS:
            raise ValueError('Native metadata exceeds its observation budget')
        payload = observation.payload()
        if native:
            if payload['field'] not in FIELDS:
                raise ValueError('Unsupported native metadata field')
            if payload['unit'] is not None and (not isinstance(payload['unit'], str)
                    or not 0 < len(payload['unit'].encode()) <= 256):
                raise ValueError('Native metadata unit must be bounded text')
        grouped.setdefault((payload['scope'], payload['entity_id']), []).append(payload)
        prepared.append(payload)
    return prepared, grouped


def source_key(provider, scope, entity):
    return json.dumps([provider, scope, entity], separators=(',', ':'), ensure_ascii=False)


def legacy_rows(subject, provider, prepared, run_id, now):
    rows = []
    for payload in prepared:
        payload = dict(payload)
        value = json.dumps(payload.pop('value'), sort_keys=True, allow_nan=False)
        field = payload.pop('field')
        payload['delivery_id'] = run_id
        provenance = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        delivery = json.dumps([run_id, subject['entry_id'], provider, field, value, provenance], separators=(',', ':'))
        key = hashlib.sha256(delivery.encode()).hexdigest()
        rows.append((subject['entry_id'], subject['recording_id'], field, provider, value, now, provenance, key))
    return rows


def installed_head(previous, legacy, body):
    if previous.get('run_id') is not None and previous.get('at') is not None:
        raw = previous.get('raw')
        return {'run_id': previous['run_id'], 'accepted_at': previous['at'],
                'result_hash': result_hash(raw) if isinstance(raw, list) else None}
    if legacy is None:
        return None
    raw = body.get('observations') if isinstance(body, dict) and body.get('format') == FORMAT else None
    return {'run_id': legacy['delivery_key'], 'accepted_at': legacy['observed_at'],
            'result_hash': result_hash(raw) if isinstance(raw, list) else None}


def runtime_update(subject, provider, source, previous, head, values, run_id, now):
    normalized = {payload['field']: profile_value(payload) for payload in values}
    digest = result_hash(values)
    replay = head is not None and head['run_id'] == run_id
    if replay:
        changed = (head['result_hash'] != digest if head['result_hash'] is not None else
            any(previous.get('observations', {}).get(name) != value for name, value in normalized.items()))
        if changed:
            raise ValueError('A native metadata run cannot change its result')
    elif head is not None and now <= head['accepted_at']:
        return None
    current = {**previous.get('observations', {}), **normalized}
    changed = source is None or current != previous.get('observations', {})
    return {'revision': (source['revision'] + 1 if source else 1) if changed else None,
            'document': {'observations': current, 'raw': values, 'entry_id': subject['entry_id'],
                'recording_id': subject['recording_id'], 'provider': provider, 'run_id': run_id, 'at': now} if changed else None,
            'head': None if replay else {'run_id': run_id, 'accepted_at': now, 'result_hash': digest}}
