"""Current catalog field ownership and exact source-entry identity.

Numbered migrations keep independent copies of their historical definitions. New catalog fields
belong here and in a new schema migration, never in a previously shipped migration's field maps.
"""
import hashlib
import json

RECORDING_FIELDS = (
    'genre', 'bpm', 'energy', 'danceability', 'mb_recording_id', 'music_key', 'music_scale',
    'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental',
)
ITEM_FIELDS = (
    'title', 'artist', 'orig_title', 'orig_artist', 'album', 'duration_s', 'available',
    'video_type', 'artist_browse_id', 'album_browse_id', 'thumbnail', 'mb_year',
    'loudness', 'dynamic_complexity', 'popularity', 'gain', 'label',
)


def unresolved_ref(album, title, artist):
    """Exact source entry, compatible with existing catalog IDs; never a recording match.

    A future signature format needs a distinct prefix and an explicit upgrade/lookup path.
    Changing text normalization in place would create duplicate entries on the next observation.
    """
    raw = json.dumps([album or '', title or '', artist or ''], ensure_ascii=False)
    return 'unresolved:' + hashlib.sha256(raw.encode()).hexdigest()
