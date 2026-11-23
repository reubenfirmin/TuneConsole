"""Selected provider history and corrections owned by the affected recording."""
from . import worker_settings

VIEW = 'recording_metadata_history'
SELECTIONS = 'metadata_provider_history'
CORRECTIONS = 'metadata_corrections'
CORRECTED_FIELDS = 'metadata_corrected_fields'
SERIAL = 'metadata_history_high_water'


def event_sql(identity, entry, field, source, operation, value, genre, at):
    return f'''json_array({identity},{entry},{field},{source},{operation},
        CASE typeof({value}) WHEN 'real' THEN json(printf('%!0.26g',{value}))
             WHEN 'blob' THEN json_object('hex',hex({value})) ELSE {value} END,
        {genre},json(printf('%!0.26g',{at})))'''


def value_sql(event):
    value = f'{event}->5'
    return f"CASE json_type({value}) WHEN 'object' THEN unhex(({value})->>'hex') ELSE {event}->>5 END"


def events_sql(column):
    return f'''SELECT event.value->>0 AS id,event.value->>1 AS entry_id,r.id AS recording_id,
        event.value->>2 AS field,event.value->>3 AS source,event.value->>4 AS operation,
        {value_sql('event.value')} AS value,event.value->>6 AS genre_key,event.value->>7 AS changed_at
        FROM recordings r CROSS JOIN json_each(r.{column}) event WHERE r.{column}<>'[]' '''


def append(conn, entry, recording, field, source, operation, value, at, *, genre_key=None):
    if operation not in ('select', 'edit', 'resolve', 'reset') or at is None:
        raise ValueError('Invalid metadata history event')
    identity = int(worker_settings.read(conn, SERIAL, '0')) + 1
    worker_settings.save(conn, SERIAL, str(identity))
    owner_column = SELECTIONS if operation == 'select' else CORRECTIONS
    if operation != 'select':
        conn.execute(f'UPDATE track_sources SET {CORRECTED_FIELDS}=json_patch({CORRECTED_FIELDS},json_object(?,1)) '
                     f'WHERE id=? AND ({CORRECTED_FIELDS}->>?) IS NULL', (field, entry, field))
    event = event_sql(*(':identity', ':entry', ':field', ':source', ':operation', ':value', ':genre', ':at'))
    updated = conn.execute(f"UPDATE recordings SET {owner_column}=json_insert({owner_column},'$[#]',{event}) WHERE id=:recording",
        dict(identity=identity, entry=entry, field=field, source=source, operation=operation, value=value,
             genre=genre_key, at=at, recording=recording)).rowcount
    if updated != 1:
        raise ValueError('Unknown recording for metadata history')
