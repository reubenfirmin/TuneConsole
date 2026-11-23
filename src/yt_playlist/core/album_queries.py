"""Shared reads over album ownership and currently published tracklists."""

PRIMARY_IDS = "(SELECT provider,provider_id,album_id FROM album_provider_ids WHERE is_primary=1)"

FETCH_STATE = """(
    SELECT a.id album_id,t.fetched_at,1 complete,t.expected_count expected_track_count
    FROM albums a JOIN album_tracklists t ON t.id=a.current_tracklist_id WHERE t.status='complete'
)"""

TRACK_LINKS = """(
    SELECT min(t.id) id,p.provider_id browse_id,e.recording_id,t.entry_id,e.video_id,min(t.source) source
    FROM album_tracks t JOIN album_provider_ids p ON p.album_id=t.album_id AND p.provider='youtube'
    JOIN track_sources e ON e.id=t.entry_id GROUP BY p.provider_id,t.entry_id
)"""

SAVED = f"""(
    SELECT p.provider_id browse_id,a.title,a.artist,a.year,a.type,a.thumbnail
    FROM albums a JOIN {PRIMARY_IDS} p ON p.album_id=a.id AND p.provider='youtube'
    WHERE EXISTS(SELECT 1 FROM saved_album_memberships m WHERE m.album_id=a.id)
)"""
