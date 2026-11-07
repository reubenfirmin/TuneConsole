"""Typed identifier evidence linked to the result that actually supplied it."""
from . import metadata_evidence as evidence


def record(conn, entry_id, provider, field, value, delivery_id, *, observation_id=None, now=None):
    if not value or field not in ('mb_recording_id', 'isrc'):
        return
    scheme = 'musicbrainz' if field == 'mb_recording_id' else 'isrc'
    supplied = value if isinstance(value, (list, tuple)) else [value]
    if len(supplied) > 1024 or any(not isinstance(identifier, str) or not 0 < len(identifier.encode()) <= 4096
                                  for identifier in supplied):
        raise ValueError('Identifier evidence requires bounded nonempty text')
    now = evidence._clock(now)
    with evidence._atomic(conn):
        subject = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        delivery = conn.execute('SELECT provider,input_entry_id,input_recording_id,input_video_id,observed_at '
                                'FROM metadata_deliveries WHERE id=?', (delivery_id,)).fetchone()
        if subject is None or delivery is None or tuple(delivery[:4]) != (provider, entry_id, *tuple(subject)):
            raise ValueError('Identifier evidence has a different delivery or captured input')
        if field == 'mb_recording_id' and observation_id is None:
            raise ValueError('A recording MBID requires its supplying catalog observation')
        for identifier in supplied:
            conn.execute('INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,input_video_id,status,'
                "observed_at,delivery_id,observation_id) VALUES (?,?,'recording',?,?,?,'candidate',?,?,?) "
                'ON CONFLICT(delivery_id,scheme,entity_type,value) WHERE delivery_id IS NOT NULL DO NOTHING',
                (subject['recording_id'], scheme, identifier, provider, subject['video_id'],
                 delivery['observed_at'], delivery_id, observation_id if field == 'mb_recording_id' else None))
