"""Explicit setup data for tests whose subject is not provider selection.

Use the real provider/reconciliation APIs when testing enrichment behavior. These
helpers only construct catalog metadata and completed recording jobs for consumers.
They deliberately live outside production repositories.
"""
from yt_playlist.core import catalog, metadata


def use_existing_album_tracks(store, monkeypatch):
    """Keep release comparison real while supplying controlled catalog inputs."""
    from tests.conftest import seed_catalog_track
    from yt_playlist.util.matching import identity_key

    def observe(video, title, artist, **fields):
        row = store.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()
        if row is not None:
            return row[0]
        entry = seed_catalog_track(store, identity_key(title, artist), title, artist, video_id=video)
        with store.conn:
            # A fetched release supplies catalog entries, not library ownership.
            store.conn.execute('DELETE FROM library_memberships WHERE entry_id=?', (entry,))
            store.conn.execute('UPDATE youtube_items SET album=?,album_browse_id=?,duration_s=? WHERE video_id=?',
                (fields.get('album'), fields.get('album_browse_id'), fields.get('duration_s'), video))
        return entry

    monkeypatch.setattr(store.tracks, 'observe_track_entry', observe)


def seed_enrichment(store, entry, genre, year):
    with store._lock, store.conn:
        metadata.fill(store.conn, catalog.resolve_entry(store.conn, entry),
                      {'genre': genre, 'mb_year': year}, 'fixture', blank=True)


def seed_audio(store, entry, **fields):
    fields = {field: value for field, value in fields.items() if value is not None}
    with store._lock, store.conn:
        entry = catalog.resolve_entry(store.conn, entry)
        catalog.replace_hints(store.conn, entry, fields)
        metadata.fill(store.conn, entry, fields, 'fixture')


def seed_discovered_enrichment(store, key, genre, year):
    _seed_discovered(store, key, {'genre': genre, 'mb_year': year})


def seed_discovered_audio(store, key, **fields):
    _seed_discovered(store, key, fields)


def _seed_discovered(store, key, fields):
    with store._lock, store.conn:
        row = store.conn.execute('SELECT id FROM discovered_tracks WHERE identity_key=?', (key,)).fetchone()
        if row is not None:
            for field, value in fields.items():
                if value is not None:
                    metadata.select(store.conn, row['id'], field, value, 'fixture')


def complete_entries(store, entries, now):
    with store._lock:
        recordings = {store.tracks.catalog_subject(entry)['recording_id'] for entry in entries}
        store.enrichment_queue.mark_recordings_enriched(recordings, now)


def complete_discovered(store, keys, now):
    with store._lock:
        rows = store.discovery.discovered_tracks_by_keys(keys)
        store.enrichment_queue.mark_recordings_enriched({row['recording_id'] for row in rows.values()}, now)


def choose_field(store, entry, field, value):
    with store._lock, store.conn:
        catalog.choose_field(store.conn, catalog.resolve_entry(store.conn, entry),
                             'mb_year' if field == 'year' else field,
                             value, 'conflict_resolution')
