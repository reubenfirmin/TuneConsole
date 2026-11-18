"""Current-format stores and ordinary provider evidence for metadata tests."""
from tests.store_fixture_support import file_consumer_store as catalog
from yt_playlist.core import metadata_deliveries as deliveries, metadata_evidence as evidence


def capture(store, video='video', key='capture:1', *, fields=None):
    fields = {'bpm': 120} if fields is None else fields
    entry = store.tracks.upsert_track(video, 'Song', 'Artist', None, 200)
    recording, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
    delivery = deliveries.Delivery('provider', key, 'item', video, 20, entry, recording, video, response=fields)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        owner, = deliveries.record_many(store.conn, [delivery])
        result = evidence.record_provider(store.conn, owner, fields, units={'bpm': 'bpm'} if 'bpm' in fields else {})
    return entry, recording, owner, result


def mutate(store, call):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        return call()
