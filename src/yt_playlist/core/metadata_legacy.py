"""Lossless, bounded conversion of the existing metadata evidence owners.

Old rows are independent captures unless their original producer recorded a
stronger delivery identity. Neither equal values nor nearby timestamps prove
that a finding and a log are the same observation.
"""
import json
import math

from . import metadata_deliveries as deliveries, metadata_values as values, metadata_native
from .activity import check_cancellable_work

FORMAT = 'legacy-metadata-record-1'
OBSERVATION_OWNERS = {'metadata_findings': 'legacy_finding', 'enrichment_log': 'legacy_log',
                      'metadata_decisions': 'legacy_choice', 'entry_field_choices': 'legacy_choice'}
CATALOG_FIELDS = (*values.RECORDING_FIELDS, *values.ITEM_FIELDS)
ATTEMPT_FIELDS = (*CATALOG_FIELDS, 'year', 'art', 'isrc', 'mb_release_ids', 'cover_url')
ADDED_COLUMNS = {'provider_attempts': {'delivery_id'}, 'recording_identifiers': {'delivery_id', 'observation_id'}}
UNSET = object()


def _clock(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _observed_at(row):
    return _clock(row.get('observed_at', row.get('created_at', row.get('chosen_at', row.get('updated_at', row.get('at'))))))


def _subject(conn, row):
    entry = row.get('entry_id', row.get('track_id'))
    recording = row.get('recording_id')
    video = row.get('input_video_id')
    # Existing recorded keys establish history. Missing keys do not acquire a
    # current recording/video by following today's catalog association.
    if entry is not None and not conn.execute('SELECT 1 FROM catalog_entries WHERE id=?', (entry,)).fetchone():
        entry = None
    if recording is not None and not conn.execute('SELECT 1 FROM recordings WHERE id=?', (recording,)).fetchone():
        recording = None
    if video is not None and not conn.execute('SELECT 1 FROM youtube_items WHERE video_id=?', (video,)).fetchone():
        video = None
    return entry, recording, video


def _field(row):
    return 'mb_year' if row.get('field') == 'year' else row.get('field')


def _value(conn, field, raw, *, encoded, unit=None):
    try:
        value = json.loads(raw) if encoded else raw
        if not encoded and field in values.NUMBER_FIELDS and isinstance(value, str) and value != '':
            # The old log writer used str(value). Decode its numeric syntax;
            # malformed/nonfinite text stays in its exact original delivery.
            value = json.loads(value)
        unit = (unit or values.DEFAULT_UNIT.get(field)) if value not in (None, '') else None
        return values.prepare(conn, field, value, unit=unit)
    except (ValueError, TypeError, OverflowError):
        return values.Value('unresolved', resolution_reason='unsupported_legacy_value')


def _native(row):
    try:
        provenance = json.loads(row.get('provenance_json') or 'null')
        if (isinstance(provenance, dict) and provenance.get('schema') == 1
                and provenance.get('scope') in ('recording', 'release', 'artist', 'genre', 'item')
                and isinstance(provenance.get('entity_id'), str) and provenance['entity_id']
                and row['field'] in metadata_native.FIELDS
                and provenance.get('kind') in ('reported', 'measurement', 'prediction', 'curated', 'inherited', 'derived')
                and (provenance.get('unit') is None or isinstance(provenance['unit'], str)
                     and 0 < len(provenance['unit'].encode()) <= 256)):
            return provenance
    except (ValueError, TypeError):
        pass
    return None


def _batches(conn, table):
    after = None
    while rows := conn.execute(f'SELECT rowid AS _legacy_id,* FROM {table}'
            + (' WHERE rowid>?' if after is not None else '') + ' ORDER BY rowid LIMIT 128',
            (after,) if after is not None else ()).fetchall():
        check_cancellable_work()
        yield [dict(row) for row in rows]
        after = rows[-1]['_legacy_id']


def _classify(conn, owner, row):
    entry, recording, video = _subject(conn, row)
    native = _native(row) if owner == 'metadata_findings' else None
    field = _field(row)
    scope = row.get('field_scope')
    if scope is None:
        scope = values.scope(field) if field in CATALOG_FIELDS else 'legacy_item'
    if native and native['scope'] == 'recording':
        scope = 'recording'
    if field in CATALOG_FIELDS:
        allowed = {values.scope(field), 'legacy_item'}
        if field in ('loudness', 'dynamic_complexity'):
            allowed.add('recording')
        if scope not in allowed:
            scope = 'legacy_item'
    if owner in ('metadata_decisions', 'entry_field_choices'):
        # These decisions were entry-local. The old stores did not capture a
        # recording association, so even a currently known entry stays legacy.
        scope = 'legacy_item'
    if owner in ('provider_attempts', 'enrichment_provider_state'):
        disposition = 'operational_state'
    elif entry is None:
        disposition = 'unproven_subject'
    elif native:
        disposition = 'native_observation'
    elif field not in CATALOG_FIELDS:
        disposition = 'unsupported_field'
    elif scope == 'legacy_item' or recording is None:
        disposition, scope = 'unproven_scope', 'legacy_item'
    else:
        disposition = 'catalog_evidence'
    if owner in (*OBSERVATION_OWNERS, 'catalog_field_hints') and field in CATALOG_FIELDS and (native is None or native['scope'] == 'recording'):
        raw = row.get('value') if owner == 'enrichment_log' else row.get('value_json')
        if _value(conn, field, raw, encoded=owner != 'enrichment_log',
                  unit=native.get('unit') if native else None).kind == 'unresolved':
            disposition = 'invalid_value'
    return entry, recording, video, field, scope, native, disposition


def capture_batch(conn, owner, rows):
    """Capture original row values before any legacy owner can be retired."""
    prepared, captures = [], []
    for row in rows:
        entry, recording, video, field, scope, native, disposition = _classify(conn, owner, row)
        legacy_id = row['_legacy_id']
        original = {key: value for key, value in row.items() if key != '_legacy_id' and key not in ADDED_COLUMNS.get(owner, ())}
        at = _observed_at(row)
        entity_type, entity_id = (native['scope'], native['entity_id']) if native else ('legacy_unknown', None)
        provider = row.get('provider') or row.get('source') or 'legacy_unknown'
        capture_entry = entry if recording is not None else None
        captures.append(deliveries.Delivery(provider, f'{owner}:{legacy_id}', entity_type, entity_id, at,
            capture_entry, recording if capture_entry is not None else None, video if capture_entry is not None else None,
            key_contract='legacy_record', response={'format': FORMAT, 'owner': owner, 'row': original}))
        prepared.append((row, entry, recording, video, field, scope, native, disposition))
    ids = deliveries.record_many(conn, captures)
    result = []
    for delivery, prepared_row in zip(ids, prepared):
        row, entry, recording, video, field, scope, native, disposition = prepared_row
        conn.execute('INSERT INTO metadata_legacy_records(legacy_owner,legacy_id,delivery_id,entry_id,recording_id,input_video_id,disposition) '
                     'VALUES (?,?,?,?,?,?,?) ON CONFLICT(legacy_owner,legacy_id) DO NOTHING',
                     (owner, row['_legacy_id'], delivery, entry, recording, video, disposition))
        result.append((delivery, *prepared_row))
    return result


def _observe(conn, owner, captured, *, raw=UNSET, encoded=None, source=None, origin=None, position=0):
    delivery, row, entry, recording, video, field, scope, native, _ = captured
    if (entry is None and not (owner in ('provider_attempts', 'enrichment_provider_state') and recording is not None)
            or field not in CATALOG_FIELDS):
        return None
    if native and native['scope'] != 'recording':
        return None
    if scope != 'legacy_item' and recording is None:
        scope = 'legacy_item'
    if raw is UNSET:
        raw = row.get('value_json') if owner in ('metadata_findings', 'metadata_decisions', 'entry_field_choices') else row.get('value')
    if encoded is None:
        encoded = owner in ('metadata_findings', 'metadata_decisions', 'entry_field_choices')
    value = _value(conn, field, raw, encoded=encoded, unit=native.get('unit') if native else None)
    origin = origin or OBSERVATION_OWNERS[owner]
    existing = conn.execute('SELECT id FROM metadata_observations WHERE origin=? AND legacy_owner=? AND legacy_id=? '
                            'AND legacy_position=?', (origin, owner, row['_legacy_id'], position)).fetchone()
    if existing is not None:
        return existing[0]
    at = _observed_at(row)
    conn.execute('INSERT INTO metadata_observations(delivery_id,entry_id,recording_id,input_video_id,field_scope,field,'
        'value_kind,text_value,number_value,genre_key,unit,resolution_reason,observed_at,origin,legacy_source,'
        'source_run_id,legacy_reference,legacy_owner,legacy_id,legacy_position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(origin,legacy_owner,legacy_id,legacy_position) WHERE legacy_owner IS NOT NULL DO NOTHING',
        (delivery, entry, recording, video, scope, field, value.kind, value.text, value.number,
         value.genre_key, value.unit, value.resolution_reason, at, origin,
         source if source is not None else row.get('provider', row.get('source')), row.get('run_id'), row.get('legacy_ref'),
         owner, row['_legacy_id'], position))
    return conn.execute('SELECT id FROM metadata_observations WHERE origin=? AND legacy_owner=? AND legacy_id=? '
                        'AND legacy_position=?', (origin, owner, row['_legacy_id'], position)).fetchone()[0]


def _native_observe(conn, captured):
    delivery, row, entry, recording, _, _, _, native, _ = captured
    if native is None:
        return
    if conn.execute('SELECT 1 FROM native_metadata_observations WHERE legacy_finding_id=?', (row['_legacy_id'],)).fetchone() is None:
        conn.execute('INSERT INTO native_metadata_observations(delivery_id,position,field,kind,unit,legacy_finding_id) '
                     'VALUES (?,0,?,?,?,?)', (delivery, row['field'], native['kind'], native.get('unit'), row['_legacy_id']))
    if native['scope'] not in ('recording', 'release', 'artist'):
        return
    key = json.dumps([row['provider'], native['scope'], native['entity_id']], ensure_ascii=False, separators=(',', ':'))
    # v50 already owns the current source/link projection. Backfill only its
    # field evidence pointers; do not manufacture a new source revision/fan-out.
    if not conn.execute('SELECT 1 FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone():
        return
    selected = conn.execute('SELECT o.legacy_finding_id FROM musical_source_observations s '
                            'JOIN native_metadata_observations o ON o.id=s.observation_id '
                            'WHERE s.source_key=? AND s.field=?', (key, row['field'])).fetchone()
    if selected is not None and (selected[0] is None or selected[0] > row['_legacy_id']):
        return
    observation = conn.execute('SELECT id FROM native_metadata_observations WHERE legacy_finding_id=?',
                               (row['_legacy_id'],)).fetchone()[0]
    conn.execute('INSERT INTO musical_source_observations(source_key,field,observation_id) VALUES (?,?,?) '
                 'ON CONFLICT(source_key,field) DO UPDATE SET observation_id=excluded.observation_id',
                 (key, row['field'], observation))


def backfill_observations(conn):
    """Additive backfill; retirement waits for every runtime consumer cutover."""
    counts = {}
    for owner in OBSERVATION_OWNERS:
        count = 0
        for rows in _batches(conn, owner):
            for captured in capture_batch(conn, owner, rows):
                observation = _observe(conn, owner, captured)
                if owner == 'entry_field_choices':
                    if observation is None:
                        raise ValueError('Unsupported active legacy metadata choice: ' + str(captured[1]['_legacy_id']))
                    kind = conn.execute('SELECT value_kind FROM metadata_observations WHERE id=?',
                                        (observation,)).fetchone()[0]
                    if kind == 'unresolved':
                        # An active malformed choice cannot silently disappear
                        # beneath the typed projection. The outer migration
                        # rolls back, retaining its installed current value.
                        raise ValueError('Unsupported active legacy metadata choice: ' + str(captured[1]['_legacy_id']))
                    conn.execute('INSERT INTO metadata_choice_heads(entry_id,field,observation_id) VALUES (?,?,?) '
                                 'ON CONFLICT(entry_id,field) DO NOTHING', (captured[2], captured[5], observation))
                if owner == 'metadata_findings':
                    _native_observe(conn, captured)
                count += 1
        counts[owner] = count
    return counts


def backfill_selections(conn):
    """Provider labels/times are retained attribution, never an equal-value join."""
    for old, new, subject, allowed in (
            ('recording_field_sources', 'recording_metadata_selections', 'recording_id', values.RECORDING_FIELDS),
            ('item_field_sources', 'entry_metadata_selections', 'entry_id', values.ITEM_FIELDS)):
        for row in conn.execute(f'SELECT * FROM {old}'):
            if row['field'] not in allowed:
                raise ValueError(f'Unsupported legacy metadata selection field: {old}.{row["field"]}')
            conn.execute(f'INSERT INTO {new}({subject},field,attribution_status,legacy_source,selected_at) '
                         "VALUES (?,?,'legacy_unresolved',?,?) ON CONFLICT DO NOTHING",
                         (row[subject], row['field'], row['source'], row['selected_at']))


def backfill_conflicts(conn):
    """Retain each original conflict and link its explicitly offered evidence.

    A recorded resolution is its own decision. It is never matched to an equal
    candidate value to invent the provider observation that the old code chose.
    """
    statuses = {0: 'unresolved', 1: 'user_resolved', 2: 'provider_converged'}
    count = 0
    for rows in _batches(conn, 'enrichment_conflict'):
        for captured in capture_batch(conn, 'enrichment_conflict', rows):
            _, row, entry, recording, _, field, scope, _, _ = captured
            if entry is None or field not in CATALOG_FIELDS:
                continue
            if type(row['resolved']) is not int or row['resolved'] not in statuses:
                raise ValueError('Unsupported legacy conflict status: ' + str(row['_legacy_id']))
            previous = conn.execute('SELECT id FROM metadata_conflicts WHERE legacy_record_id=?',
                                    (row['_legacy_id'],)).fetchone()
            if previous is not None:
                count += 1
                continue
            selected = None
            reason = None
            if row['resolved'] == 1:
                if row.get('resolved_value') is None:
                    reason = 'unrecorded_legacy_resolution'
                else:
                    selected = _observe(conn, 'enrichment_conflict', captured,
                        raw=row['resolved_value'], encoded=False, origin='legacy_choice')
                    value = conn.execute('SELECT value_kind FROM metadata_observations WHERE id=?',
                                         (selected,)).fetchone()
                    if value['value_kind'] == 'unresolved':
                        selected, reason = None, 'unsupported_legacy_value'
            conflict = conn.execute('INSERT INTO metadata_conflicts(entry_id,recording_id,field_scope,field,'
                'status,selected_observation_id,updated_at,legacy_record_id,legacy_resolution_reason) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (None if scope == 'recording' else entry, recording if scope == 'recording' else None,
                 scope, field, statuses[row['resolved']], selected, _clock(row['updated_at']),
                 row['_legacy_id'], reason)).lastrowid
            try:
                candidates = json.loads(row['candidates'])
            except (ValueError, TypeError):
                candidates = None
            if not isinstance(candidates, list):
                # The exact malformed body remains explained by the immutable
                # legacy record. Do not manufacture a parseable candidate.
                continue
            for position, candidate in enumerate(candidates):
                if (not isinstance(candidate, dict) or not isinstance(candidate.get('provider'), str)
                        or not candidate['provider'] or 'value' not in candidate):
                    continue
                observation = _observe(conn, 'enrichment_conflict', captured,
                    raw=candidate['value'], encoded=False, source=candidate['provider'],
                    origin='legacy_conflict', position=position)
                conn.execute('INSERT INTO metadata_conflict_candidates(conflict_id,observation_id,position) VALUES (?,?,?)',
                             (conflict, observation, position))
            count += 1
    return count


def _operational_fields(conn, owner, captured):
    row = captured[1]
    try:
        fields = json.loads(row['fields_json'])
    except (ValueError, TypeError) as exc:
        raise ValueError(f'Invalid legacy provider fields: {owner}:{row["_legacy_id"]}') from exc
    if not isinstance(fields, dict):
        raise ValueError(f'Invalid legacy provider fields: {owner}:{row["_legacy_id"]}')
    for position, (field, raw) in enumerate(fields.items()):
        if field not in ATTEMPT_FIELDS:
            raise ValueError(f'Unsupported legacy provider field: {owner}.{field}')
        canonical = 'mb_year' if field == 'year' else field
        observation = None
        if canonical in CATALOG_FIELDS:
            scoped = (*captured[:5], canonical, 'legacy_recording', *captured[7:])
            observation = _observe(conn, owner, scoped, raw=raw, encoded=False,
                origin='legacy_attempt' if owner == 'provider_attempts' else 'legacy_state', position=position)
        yield field, observation


def backfill_operational(conn):
    """Keep retry/frontier membership relational, with explicit legacy values.

    The old attempt/state owners recorded a recording and sometimes an item,
    never the entry used by the request. Those unknown inputs remain unknown;
    their finite evidence cannot be selected as a newly proved catalog fact.
    """
    counts = {}
    for owner in ('provider_attempts', 'enrichment_provider_state'):
        count = 0
        for rows in _batches(conn, owner):
            for captured in capture_batch(conn, owner, rows):
                delivery, row, _, recording, *_ = captured
                if recording is None:
                    raise ValueError(f'Unproven legacy provider subject: {owner}:{row["_legacy_id"]}')
                if owner == 'provider_attempts':
                    conn.execute('UPDATE provider_attempts SET delivery_id=? WHERE id=? AND delivery_id IS NULL',
                                 (delivery, row['id']))
                for field, observation in _operational_fields(conn, owner, captured):
                    if owner == 'provider_attempts':
                        conn.execute('INSERT INTO provider_attempt_fields(attempt_id,field,observation_id) VALUES (?,?,?) '
                                     'ON CONFLICT DO NOTHING', (row['id'], field, observation))
                    else:
                        conn.execute('INSERT INTO provider_state_fields(recording_id,provider,field,delivery_id,observation_id) '
                            'VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING', (recording, row['provider'], field, delivery, observation))
                count += 1
        counts[owner] = count
    return counts


def backfill_identifiers(conn):
    count = 0
    for rows in _batches(conn, 'recording_identifiers'):
        for delivery, row, *_ in capture_batch(conn, 'recording_identifiers', rows):
            conn.execute('UPDATE recording_identifiers SET delivery_id=? WHERE id=? AND delivery_id IS NULL',
                         (delivery, row['id']))
            count += 1
    return count


def backfill_hints(conn):
    for rows in _batches(conn, 'catalog_field_hints'):
        for captured in capture_batch(conn, 'catalog_field_hints', rows):
            _, row, entry, _, _, field, *_ = captured
            observation = _observe(conn, 'catalog_field_hints', captured, raw=row['value_json'], encoded=True,
                                   origin='legacy_hint')
            if observation is not None:
                conn.execute('INSERT INTO metadata_hints(entry_id,field,source,observation_id,active) '
                             'VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
                             (entry, field, row['source'], observation, row['active']))
