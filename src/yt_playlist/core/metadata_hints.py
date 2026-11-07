"""Finite imported hints, separate from current selections and user decisions."""
from . import metadata_deliveries as deliveries, metadata_evidence as evidence, metadata_values as values


def put(conn, entry_id, field, source, value, *, now=None):
    from . import metadata_runtime
    if metadata_runtime.active(conn):
        return
    values.scope(field)
    if not isinstance(source, str) or not source:
        raise ValueError('A metadata hint requires its import source')
    with evidence._atomic(conn):
        if conn.execute('SELECT 1 FROM metadata_hints WHERE entry_id=? AND field=? AND source=?',
                        (entry_id, field, source)).fetchone():
            return
        delivery = deliveries.capture_state(conn, entry_id, 'catalog_import', 'hint:' + field + ':' + source,
            {'format': 'catalog-import-hint-1', 'source': source, 'field': field, 'value': value}, now=evidence._clock(now))
        units = {field: values.DEFAULT_UNIT[field]} if field in values.NUMBER_FIELDS and value not in (None, '') else {}
        observation = evidence.record_provider(conn, delivery, {field: value}, units=units)[field]
        conn.execute('INSERT INTO metadata_hints(entry_id,field,source,observation_id) VALUES (?,?,?,?)',
                     (entry_id, field, source, observation))


def matching(conn, entry_id, recording_id, field, value):
    rows = conn.execute('SELECT h.*,o.value_kind,o.text_value,o.number_value,o.genre_key,o.unit,o.resolution_reason '
        'FROM metadata_hints h JOIN metadata_observations o ON o.id=h.observation_id '
        'JOIN catalog_entries e ON e.id=h.entry_id WHERE h.active=1 AND h.field=? AND '
        + ('e.recording_id=?' if field in values.RECORDING_FIELDS else 'e.id=?'),
        (field, recording_id if field in values.RECORDING_FIELDS else entry_id)).fetchall()
    if any(evidence.value_from_row(row).selected() == value for row in rows if row['value_kind'] != 'unresolved'):
        return rows
    return []
