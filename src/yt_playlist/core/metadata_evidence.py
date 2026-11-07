"""Finite metadata evidence admission and explicit current selection ownership.

The existing catalog retains its typed columns. These helpers do not turn
native provider documents into arbitrary attributes or change provider policy.
"""
from collections.abc import Mapping
from contextlib import contextmanager
import sqlite3

from . import metadata_values as values
from .metadata_contract import clock as _clock


def available(conn):
    from . import metadata_runtime
    if metadata_runtime.active(conn):
        return False
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='metadata_observations'").fetchone() is not None


@contextmanager
def _atomic(conn):
    if not conn.in_transaction:
        raise RuntimeError('Metadata evidence mutation needs an outer transaction')
    conn.execute('SAVEPOINT metadata_evidence_mutation')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK TO metadata_evidence_mutation')
        raise
    finally:
        conn.execute('RELEASE metadata_evidence_mutation')


def value_from_row(row):
    return values.Value(*(row[name] for name in ('value_kind', 'text_value', 'number_value', 'genre_key', 'unit', 'resolution_reason')))


def record_provider(conn, delivery_id, fields, *, units=None, source_run_id=None):
    """Return field-to-observation IDs for one concrete catalog delivery."""
    if not isinstance(fields, Mapping) or len(fields) > len(values.RECORDING_FIELDS) + len(values.ITEM_FIELDS):
        raise ValueError('Metadata evidence exceeds its finite field budget')
    if source_run_id is not None and (not isinstance(source_run_id, str) or not 0 < len(source_run_id.encode()) <= 4096):
        raise ValueError('Metadata run identity must be bounded text')
    units = units or {}
    if not isinstance(units, Mapping) or units.keys() - fields.keys():
        raise ValueError('Metadata units must belong to the supplied fields')
    prepared = {field: values.prepare(conn, field, value, unit=units.get(field)) for field, value in fields.items()}
    return record_prepared(conn, delivery_id, prepared, source_run_id=source_run_id)


def record_prepared(conn, delivery_id, prepared, *, source_run_id=None):
    """Persist validated values with their delivery and idempotency constraints."""
    result = {}
    with _atomic(conn):
        delivery = conn.execute('SELECT input_entry_id,input_recording_id,input_video_id,observed_at '
                                'FROM metadata_deliveries WHERE id=?', (delivery_id,)).fetchone()
        if delivery is None or delivery['input_entry_id'] is None or delivery['observed_at'] is None:
            raise ValueError('Catalog metadata requires a concrete timed input delivery')
        for field, value in prepared.items():
            row = conn.execute("SELECT * FROM metadata_observations WHERE delivery_id=? AND field=? "
                               "AND field_scope=? AND origin='provider'", (delivery_id, field, values.scope(field))).fetchone()
            if row:
                captured = value_from_row(row)
                if ((captured.kind, captured.text, captured.number, captured.unit) != (value.kind, value.text, value.number, value.unit)
                        or source_run_id is not None and row['source_run_id'] != source_run_id):
                    raise sqlite3.IntegrityError('A metadata delivery cannot change its field evidence')
                result[field] = row['id']
            else:
                result[field] = conn.execute('INSERT INTO metadata_observations(delivery_id,entry_id,recording_id,'
                    'input_video_id,field_scope,field,value_kind,text_value,number_value,genre_key,unit,resolution_reason,'
                    "observed_at,source_run_id,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'provider')",
                    (delivery_id, *tuple(delivery)[:3], values.scope(field), field, value.kind, value.text,
                     value.number, value.genre_key, value.unit, value.resolution_reason, delivery['observed_at'], source_run_id)).lastrowid
    return result


def select(conn, entry_id, field, observation_id, *, now=None):
    """Change a typed base value and its exact lineage together."""
    expected_scope, now = values.scope(field), _clock(now)
    with _atomic(conn):
        subject = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        row = conn.execute('SELECT * FROM metadata_observations WHERE id=?', (observation_id,)).fetchone()
        if (subject is None or row is None or row['field'] != field or row['field_scope'] != expected_scope
                or row['recording_id'] != subject['recording_id']
                or expected_scope != 'recording' and row['entry_id'] != entry_id
                or row['origin'] in ('user', 'legacy_choice')):
            raise ValueError('Metadata selection has a different subject, field or scope')
        value = value_from_row(row).selected()
        if field in values.RECORDING_FIELDS:
            table, key, identity = 'recording_metadata_selections', 'recording_id', subject['recording_id']
            if field == 'genre':
                from . import genre_classifications
                genre_classifications.write_evidence(conn, 'recordings', identity, value_from_row(row))
            else:
                conn.execute(f'UPDATE recordings SET {field}=? WHERE id=? AND {field} IS NOT ?', (value, identity, value))
        else:
            table, key, identity = 'entry_metadata_selections', 'entry_id', entry_id
            if subject['video_id'] is not None:
                conn.execute(f'UPDATE youtube_items SET {field}=? WHERE video_id=? AND {field} IS NOT ?', (value, subject['video_id'], value))
            else:
                conn.execute(f'UPDATE unresolved_items SET {field}=? WHERE entry_id=? AND {field} IS NOT ?', (value, entry_id, value))
        conn.execute(f'INSERT INTO {table}({key},field,observation_id,attribution_status,selected_at) '
                     "VALUES (?,?,?,'evidenced',?) ON CONFLICT(" + key + ',field) DO UPDATE '
                     "SET observation_id=excluded.observation_id,attribution_status='evidenced',"
                     'legacy_source=NULL,selected_at=excluded.selected_at '
                     "WHERE observation_id IS NOT excluded.observation_id OR attribution_status<>'evidenced' OR legacy_source IS NOT NULL",
                     (identity, field, observation_id, now))


def transfer_recording_selection(conn, previous, target, field, *, now=None):
    """Copy a verified equivalent selection without reassociating old evidence."""
    if values.scope(field) != 'recording':
        raise ValueError('Only shared recording metadata can follow an equivalent recording')
    now = _clock(now)
    with _atomic(conn):
        selected = conn.execute('SELECT s.*,o.value_kind,o.text_value,o.number_value,o.genre_key,o.unit,'
            'o.resolution_reason,COALESCE(d.provider,o.legacy_source,s.legacy_source) AS provider '
            'FROM recording_metadata_selections s LEFT JOIN metadata_observations o ON o.id=s.observation_id '
            'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE s.recording_id=? AND s.field=?',
            (previous, field)).fetchone()
        if selected is None:
            return False
        entry = conn.execute('SELECT id,video_id FROM catalog_entries WHERE recording_id=? ORDER BY id LIMIT 1',
                             (target,)).fetchone()
        if entry is None:
            raise ValueError('A transferred selection needs an admitted target input')
        if selected['observation_id'] is None:
            # An unknown historical attribution remains unknown after a merge.
            if field == 'genre':
                from . import genre_classifications
                genre_classifications.copy_recording(conn, previous, target)
            else:
                raw = conn.execute(f'SELECT {field} FROM recordings WHERE id=?', (previous,)).fetchone()[0]
                conn.execute(f'UPDATE recordings SET {field}=? WHERE id=?', (raw, target))
            conn.execute('INSERT INTO recording_metadata_selections(recording_id,field,attribution_status,'
                "legacy_source,selected_at) VALUES (?,?,'legacy_unresolved',?,?)",
                (target, field, selected['legacy_source'], selected['selected_at']))
            return True
        observation = conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,'
            'field_scope,field,value_kind,text_value,number_value,genre_key,unit,resolution_reason,observed_at,'
            "origin,legacy_source,derived_from_observation_id) VALUES (?,?,?,'recording',?,?,?,?,?,?,?,?,"
            "'selection_transfer',?,?)", (entry['id'], target, entry['video_id'], field, selected['value_kind'],
                selected['text_value'], selected['number_value'], selected['genre_key'], selected['unit'],
                selected['resolution_reason'], now, selected['provider'], selected['observation_id'])).lastrowid
        select(conn, entry['id'], field, observation, now=now)
    return True


def reuse_recording_observation(conn, entry_id, observation_id, *, now=None):
    """Apply retained recording evidence without inventing an old input entry.

    Legacy provider state captured a recording but no input entry. A deliberate
    current reuse can derive its recording-level selection while keeping that
    original scope and missing input unchanged in the retained observation.
    """
    now = _clock(now)
    with _atomic(conn):
        row = conn.execute('SELECT o.*,d.provider FROM metadata_observations o '
                           'JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?',
                           (observation_id,)).fetchone()
        entry = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?',
                             (entry_id,)).fetchone()
        if (row is None or entry is None or row['field_scope'] != 'legacy_recording'
                or row['recording_id'] != entry['recording_id'] or row['field'] not in values.RECORDING_FIELDS
                or row['origin'] not in ('legacy_attempt', 'legacy_state')):
            raise ValueError('Retained evidence does not establish this recording field')
        value_from_row(row).selected()
        prior = conn.execute("SELECT id FROM metadata_observations WHERE origin='selection_transfer' "
            'AND derived_from_observation_id=? AND entry_id=? AND recording_id=? ORDER BY id LIMIT 1',
            (observation_id, entry_id, entry['recording_id'])).fetchone()
        if prior:
            return prior[0]
        return conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,'
            'field_scope,field,value_kind,text_value,number_value,genre_key,unit,resolution_reason,observed_at,'
            "origin,legacy_source,derived_from_observation_id) VALUES (?,?,?,'recording',?,?,?,?,?,?,?,?,"
            "'selection_transfer',?,?)", (entry_id, entry['recording_id'], entry['video_id'], row['field'],
                row['value_kind'], row['text_value'], row['number_value'], row['genre_key'], row['unit'],
                row['resolution_reason'], now, row['provider'], observation_id)).lastrowid


def choose(conn, entry_id, field, value=None, *, withdraw=False, source='user', now=None, expected_recording_id=None):
    """Retain every explicit decision, including null, blank and withdrawal."""
    from . import metadata_runtime
    if metadata_runtime.active(conn):
        return metadata_runtime.choose(conn, entry_id, field, value, withdraw=withdraw, source=source,
                                       now=now, expected_recording_id=expected_recording_id)
    now, scope = _clock(now), values.scope(field)
    if type(withdraw) is not bool or not isinstance(source, str) or not source:
        raise ValueError('Invalid metadata choice operation or source')
    prepared = values.Value('withdrawn') if withdraw else values.prepare(conn, field, value,
        unit=values.DEFAULT_UNIT.get(field) if value not in (None, '') else None)
    with _atomic(conn):
        subject = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if subject is None or expected_recording_id is not None and subject['recording_id'] != expected_recording_id:
            raise ValueError('Metadata decision input changed before capture')
        observation = conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,'
            'field_scope,field,value_kind,text_value,number_value,genre_key,unit,resolution_reason,observed_at,origin,legacy_source) '
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'user',?)", (entry_id, *tuple(subject), scope, field, prepared.kind,
             prepared.text, prepared.number, prepared.genre_key, prepared.unit, prepared.resolution_reason, now, source)).lastrowid
        conn.execute('INSERT INTO metadata_choice_heads(entry_id,field,observation_id) VALUES (?,?,?) '
                     'ON CONFLICT(entry_id,field) DO UPDATE SET observation_id=excluded.observation_id',
                     (entry_id, field, observation))
    return observation


def effective(conn, entry_id, field):
    """Use a retained explicit choice, or the current typed base after withdrawal."""
    from . import metadata_runtime
    if metadata_runtime.active(conn):
        if field not in metadata_runtime.CODES:
            raise ValueError('Unknown metadata field')
        row = conn.execute(f'SELECT {field} FROM catalog_tracks WHERE id=?', (entry_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown catalog entry')
        return row[0]
    values.scope(field)
    row = conn.execute('SELECT o.* FROM metadata_choice_heads h JOIN metadata_observations o ON o.id=h.observation_id '
                       'WHERE h.entry_id=? AND h.field=?', (entry_id, field)).fetchone()
    if row is not None and row['value_kind'] != 'withdrawn':
        if field == 'genre':
            from .genre_classifications import selected_label
            return selected_label(conn, value_from_row(row))
        return value_from_row(row).selected()
    if field in values.RECORDING_FIELDS:
        from . import genre_classifications
        expression = genre_classifications.read_expression(conn, 'r', 'recordings') if field == 'genre' else f'r.{field}'
        row = conn.execute(f'SELECT {expression} FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id '
                           'WHERE e.id=?', (entry_id,)).fetchone()
    else:
        row = conn.execute(f'SELECT CASE WHEN e.video_id IS NOT NULL THEN y.{field} ELSE u.{field} END '
                           'FROM catalog_entries e LEFT JOIN youtube_items y ON y.video_id=e.video_id '
                           'LEFT JOIN unresolved_items u ON u.entry_id=e.id WHERE e.id=?', (entry_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown catalog entry')
    return row[0]
