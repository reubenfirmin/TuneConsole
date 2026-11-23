"""Import source and activity markers on the observations they describe."""
import sqlite3

from . import metadata_deliveries as deliveries, metadata_evidence as evidence, metadata_values as values


INDEX = 'ix_metadata_observation_hints'


def sources(conn, entry_id, source):
    return {row[0] for row in conn.execute(
        f"SELECT o.field FROM metadata_observations o INDEXED BY {INDEX},json_each(o.hint_sources) h "
        "WHERE o.entry_id=? AND o.hint_sources<>'{}' AND h.key=?", (entry_id, source))}


def store(conn, entry_id, field, source, observation):
    if not isinstance(source, str) or not source:
        raise ValueError('A metadata hint requires its import source')
    if not conn.execute('SELECT 1 FROM metadata_observations WHERE id=? AND entry_id=? AND field=?',
                        (observation, entry_id, field)).fetchone():
        raise sqlite3.IntegrityError('Metadata hint has a different entry or field')
    conn.execute(
        'UPDATE metadata_observations SET hint_sources=json_patch(hint_sources,json_object(:source,1)) '
        'WHERE id=:observation AND NOT EXISTS('
        f'SELECT 1 FROM metadata_observations o INDEXED BY {INDEX},json_each(o.hint_sources) h '
        "WHERE o.entry_id=:entry AND o.field=:field AND o.hint_sources<>'{}' AND h.key=:source)",
        dict(entry=entry_id, field=field, source=source, observation=observation))


def put(conn, entry_id, field, source, value, *, now=None):
    from . import metadata_runtime
    if metadata_runtime.active(conn):
        return
    values.scope(field)
    if not isinstance(source, str) or not source:
        raise ValueError('A metadata hint requires its import source')
    with evidence._atomic(conn):
        if field in sources(conn, entry_id, source):
            return
        delivery = deliveries.capture_state(conn, entry_id, 'catalog_import', 'hint:' + field + ':' + source,
            {'format': 'catalog-import-hint-1', 'source': source, 'field': field, 'value': value}, now=evidence._clock(now))
        units = {field: values.DEFAULT_UNIT[field]} if field in values.NUMBER_FIELDS and value not in (None, '') else {}
        observation = evidence.record_provider(conn, delivery, {field: value}, units=units)[field]
        store(conn, entry_id, field, source, observation)


def active(conn, entry_id, recording_id, field):
    return conn.execute('SELECT o.entry_id,h.key AS source,o.id AS observation_id,'
        'o.value_kind,o.text_value,o.number_value,o.genre_key,o.unit,o.resolution_reason '
        f'FROM track_sources e JOIN metadata_observations o INDEXED BY {INDEX} ON o.entry_id=e.id,'
        "json_each(o.hint_sources) h WHERE o.hint_sources<>'{}' AND h.atom=1 AND o.field=? AND "
        + ('e.recording_id=?' if field in values.RECORDING_FIELDS else 'e.id=?') +
        ' ORDER BY o.entry_id,h.key',
        (field, recording_id if field in values.RECORDING_FIELDS else entry_id))


def matching(conn, entry_id, recording_id, field, value):
    rows = list(active(conn, entry_id, recording_id, field))
    if any(evidence.value_from_row(row).selected() == value for row in rows if row['value_kind'] != 'unresolved'):
        return rows
    return []


def retire_entry(conn, entry_id, field):
    conn.execute(f'UPDATE metadata_observations INDEXED BY {INDEX} '
                 'SET hint_sources=(SELECT json_group_object(h.key,0) FROM json_each(hint_sources) h) '
                 "WHERE entry_id=? AND field=? AND hint_sources<>'{}'", (entry_id, field))


def retire(conn, field, keys):
    conn.executemany(f'UPDATE metadata_observations INDEXED BY {INDEX} '
                     'SET hint_sources=json_patch(hint_sources,json_object(:source,0)) '
                     "WHERE entry_id=:entry AND field=:field AND hint_sources<>'{}' "
                     'AND EXISTS(SELECT 1 FROM json_each(hint_sources) h WHERE h.key=:source AND h.atom=1)',
                     (dict(entry=entry, field=field, source=source) for entry, source in keys))
