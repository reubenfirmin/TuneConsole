"""Provider result capture and relational retry/frontier field membership.

Durable evidence and current operational state have separate lifetimes. A state
field points to its supplying delivery; merging a retry cannot duplicate a JSON
document or manufacture an older request's missing input identity.
"""
from collections.abc import Mapping
import json
import sqlite3
import uuid

from . import metadata_deliveries as deliveries, metadata_evidence as evidence, metadata_values as values
from .activity import WorkBudgetExceeded
from .evidence_ledger import canonical as encode
from .metadata_contract import PROVIDER_FIELDS as FIELDS

FORMAT = 'enrichment-result-1'
MAX_STATE_BYTES = 8 * 1024 * 1024


def available(conn):
    from . import metadata_runtime
    if metadata_runtime.active(conn):
        return False
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='provider_state_fields'").fetchone() is not None


def canonical(field):
    return 'mb_year' if field == 'year' else field


def capture_result(conn, entry_id, provider, fields, *, run_id=None, now=None, part='result'):
    now = evidence._clock(now)
    if not isinstance(fields, Mapping) or len(fields) > len(FIELDS) or fields.keys() - set(FIELDS):
        raise ValueError('Unsupported provider result fields')
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError('A provider result needs its actual run identity')
    captured = {}
    for field, value in fields.items():
        name = canonical(field)
        if name in (*values.RECORDING_FIELDS, *values.ITEM_FIELDS):
            if name in captured and captured[name] != value:
                raise ValueError('Provider result has contradictory field aliases')
            captured[name] = value
    with evidence._atomic(conn):
        row = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown provider result input')
        recording, video = row
        delivery, = deliveries.record_many(conn, [deliveries.Delivery(provider,
            'enrichment-' + part + ':' + (run_id or uuid.uuid4().hex), 'item' if video else 'recording',
            video or 'catalog-recording:' + str(recording), now, entry_id, recording, video,
            response={'format': FORMAT, 'fields': dict(fields)})])
        units = {name: values.DEFAULT_UNIT[name] for name, value in captured.items()
                 if name in values.NUMBER_FIELDS and value not in (None, '')}
        observations = evidence.record_provider(conn, delivery, captured, units=units, source_run_id=run_id)
    return delivery, observations


def _fields(conn, delivery_id):
    body = deliveries.response(conn, delivery_id)
    if isinstance(body, dict) and body.get('format') == FORMAT:
        fields = body.get('fields')
    elif (isinstance(body, dict) and body.get('format') == 'legacy-metadata-record-1'
          and body.get('owner') in ('provider_attempts', 'enrichment_provider_state')):
        fields = json.loads(body['row']['fields_json'])
    else:
        raise ValueError('Provider fields have a different retained delivery owner')
    if not isinstance(fields, dict):
        raise ValueError('Invalid retained provider result')
    return fields


def result_evidence(conn, entry_id, provider, delivery_id, fields, *, complete=False, run_id=None):
    """Verify the supplying result, including its captured input, before reusing it."""
    subject = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    delivery = conn.execute('SELECT provider,input_entry_id,input_recording_id,input_video_id,key_contract,delivery_key '
                            'FROM metadata_deliveries WHERE id=?', (delivery_id,)).fetchone()
    if (subject is None or delivery is None or delivery['key_contract'] == 'legacy_record'
            or tuple(delivery[:4]) != (provider, entry_id, *tuple(subject))):
        raise ValueError('Provider result has a different provider or captured input')
    if run_id is not None and delivery['delivery_key'] != 'enrichment-result:' + run_id:
        raise ValueError('Provider result belongs to a different run')
    supplied = _fields(conn, delivery_id)
    if (complete and encode(supplied) != encode(fields)
            or any(field not in supplied or encode(supplied[field]) != encode(value) for field, value in fields.items())):
        raise ValueError('Provider fields differ from their supplying result')
    return {row['field']: row['id'] for row in conn.execute(
        "SELECT field,id FROM metadata_observations WHERE delivery_id=? AND origin='provider'", (delivery_id,))}


def _read_fields(conn, rows, max_bytes):
    sizes = {row['delivery_id']: row['raw_byte_size'] for row in rows}
    if any(size is None for size in sizes.values()) or sum(sizes.values()) > max_bytes:
        raise WorkBudgetExceeded('Provider state exceeds its delivery read budget')
    bodies = {delivery: _fields(conn, delivery) for delivery in sizes}
    result = {}
    for row in rows:
        body = bodies[row['delivery_id']]
        if row['field'] not in body:
            raise ValueError('Provider field reference differs from its delivery')
        result[row['field']] = body[row['field']]
    return result


def state_fields(conn, recording_id, provider, *, max_bytes=MAX_STATE_BYTES):
    rows = conn.execute('SELECT f.field,f.delivery_id,d.raw_byte_size FROM provider_state_fields f '
        'JOIN metadata_deliveries d ON d.id=f.delivery_id WHERE f.recording_id=? AND f.provider=? ORDER BY f.field',
        (recording_id, provider)).fetchall()
    return _read_fields(conn, rows, max_bytes)


def attempt_fields(conn, attempt_id, *, max_bytes=MAX_STATE_BYTES):
    rows = conn.execute('SELECT f.field,a.delivery_id,d.raw_byte_size FROM provider_attempt_fields f '
        'JOIN provider_attempts a ON a.id=f.attempt_id JOIN metadata_deliveries d ON d.id=a.delivery_id '
        'WHERE f.attempt_id=? ORDER BY f.field', (attempt_id,)).fetchall()
    return _read_fields(conn, rows, max_bytes)


def completed_genres(conn, recording_id):
    """Genre refresh queries finite evidence, without decoding provider documents."""
    rows = conn.execute('SELECT s.provider,o.text_value FROM enrichment_provider_state s '
        "LEFT JOIN provider_state_fields f ON f.recording_id=s.recording_id AND f.provider=s.provider AND f.field='genre' "
        'LEFT JOIN metadata_observations o ON o.id=f.observation_id '
        "WHERE s.recording_id=? AND s.status<>'retry' AND NOT EXISTS(SELECT 1 FROM enrichment_provider_jobs j "
        'WHERE j.recording_id=s.recording_id AND j.provider=s.provider)', (recording_id,))
    return {row['provider']: row['text_value'] for row in rows}


def reusable_evidence(conn, entry_id, provider, fields, *, now=None):
    """Follow the current state's exact field references, never equal-value matches."""
    subject = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    if subject is None:
        raise ValueError('Unknown provider reuse input')
    result = {}
    for field, value in fields.items():
        name = canonical(field)
        if name not in (*values.RECORDING_FIELDS, *values.ITEM_FIELDS):
            continue
        row = conn.execute('SELECT o.* FROM provider_state_fields s JOIN metadata_observations o '
            'ON o.id=s.observation_id WHERE s.recording_id=? AND s.provider=? AND s.field=?',
            (subject[0], provider, field)).fetchone()
        if row is None or row['value_kind'] in ('unresolved', 'withdrawn'):
            continue
        if encode(evidence.value_from_row(row).selected()) != encode(value):
            raise ValueError('Provider reuse differs from its retained field evidence')
        if name in values.RECORDING_FIELDS and row['field_scope'] == 'legacy_recording':
            result[name] = evidence.reuse_recording_observation(conn, entry_id, row['id'], now=now)
        elif (row['recording_id'] == subject[0] and row['field_scope'] == values.scope(name)
              and (name in values.RECORDING_FIELDS or row['entry_id'] == entry_id)):
            result[name] = row['id']
        # An old release/item field without its input entry stays unproven. It
        # cannot become a new item selection by following today's association.
    return result


def record(conn, entry_id, provider, fields, *, cycle, retry=False, now=None, retry_after=0, error='',
           run_id=None, delivery_id=None, observations=None):
    """Return (attempt ID, inserted); an old replay cannot replace newer state."""
    now = evidence._clock(now)
    if type(retry) is not bool:
        raise ValueError('Provider retry flag must be a boolean')
    with evidence._atomic(conn):
        subject = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if subject is None:
            raise ValueError('Unknown provider attempt input')
        if delivery_id is None:
            delivery_id, observations = capture_result(conn, entry_id, provider, fields, run_id=run_id, now=now)
        else:
            observations = result_evidence(conn, entry_id, provider, delivery_id, fields, complete=True, run_id=run_id)
        status = 'retry' if retry else 'found' if fields else 'no_match'
        previous_attempt = conn.execute('SELECT id,status FROM provider_attempts WHERE delivery_id=?', (delivery_id,)).fetchone()
        if previous_attempt is not None:
            if previous_attempt['status'] != status:
                raise sqlite3.IntegrityError('A provider attempt replay cannot change its outcome')
            return previous_attempt['id'], False
        recording, video = subject
        # The placeholder exists only while the unregistered migration is
        # staged. The final cutover removes the old fields_json column.
        has_json = any(row[1] == 'fields_json' for row in conn.execute('PRAGMA table_info(provider_attempts)'))
        columns, placeholder = (',fields_json', ",'{}'") if has_json else ('', '')
        attempt = conn.execute('INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,delivery_id'
            + columns + ') VALUES (?,?,?,?,?,?' + placeholder + ')',
            (recording, video or '', provider, now, status, delivery_id)).lastrowid
        for field in fields:
            conn.execute('INSERT INTO provider_attempt_fields(attempt_id,field,observation_id) VALUES (?,?,?)',
                         (attempt, field, observations.get(canonical(field))))
        previous = conn.execute('SELECT * FROM enrichment_provider_state WHERE recording_id=? AND provider=?',
                                (recording, provider)).fetchone()
        same_cycle = previous is not None and previous['cycle'] == cycle
        failures = (previous['failures'] if same_cycle else 0) + 1 if retry else 0
        deadline = max(now + min(3600, 30 * 2 ** min(failures - 1, 7)), retry_after) if retry else 0
        if previous is not None and not same_cycle:
            conn.execute('DELETE FROM provider_state_fields WHERE recording_id=? AND provider=?', (recording, provider))
        has_json = any(row[1] == 'fields_json' for row in conn.execute('PRAGMA table_info(enrichment_provider_state)'))
        columns, placeholder = (',fields_json', ",'{}'") if has_json else ('', '')
        conn.execute('INSERT INTO enrichment_provider_state(recording_id,provider,cycle,at,status,failures,retry_at,error'
            + columns + ') VALUES (?,?,?,?,?,?,?,?' + placeholder + ') ON CONFLICT(recording_id,provider) DO UPDATE SET '
            'cycle=excluded.cycle,at=excluded.at,status=excluded.status,failures=excluded.failures,'
            'retry_at=excluded.retry_at,error=excluded.error', (recording, provider, cycle, now, status, failures, deadline, error))
        for field in fields:
            conn.execute('INSERT INTO provider_state_fields(recording_id,provider,field,delivery_id,observation_id) '
                'VALUES (?,?,?,?,?) ON CONFLICT(recording_id,provider,field) DO UPDATE SET '
                'delivery_id=excluded.delivery_id,observation_id=excluded.observation_id',
                (recording, provider, field, delivery_id, observations.get(canonical(field))))
    return attempt, True
