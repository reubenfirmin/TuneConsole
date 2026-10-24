"""Confirmed album ratings and their account-scoped local publication."""
from yt_playlist.util.thumbnails import best_thumb


def set_saved(store, client, identity_id, browse_id, album, *, saved, fallback=None):
    """Publish membership only after the supported YouTube acknowledgement succeeds.

    Older clients return None for success; current clients can return a boolean
    or a response dictionary. Explicit rejection never changes local ownership.
    The caller owns account selection and runs this blocking operation off-loop.
    """
    audio_id = album.get('audioPlaylistId')
    if not audio_id:
        raise ValueError('YouTube Music cannot save that candidate as an album')
    result = client.rate_playlist(audio_id, 'LIKE' if saved else 'INDIFFERENT')
    confirmed = (result is None or result is True or
                 isinstance(result, dict) and result.get('status') in (None, 'STATUS_SUCCEEDED'))
    if not confirmed:
        operation = 'save' if saved else 'removal'
        raise ValueError(f'YouTube Music did not confirm the album {operation}')
    if not saved:
        store.collection.remove_saved_album(browse_id, identity_id=identity_id)
        return
    fallback = fallback or {}
    artist = ', '.join(a['name'] for a in (album.get('artists') or []) if a.get('name'))
    store.collection.add_saved_album({
        'browse': browse_id, 'title': album.get('title') or fallback.get('title'),
        'artist': artist or fallback.get('artist') or '',
        'year': album.get('year') or fallback.get('year'), 'type': album.get('type'),
        'thumbnail': best_thumb(album.get('thumbnails')) or fallback.get('thumbnail'),
        'credits': album.get('artists'),
    }, identity_id=identity_id)
