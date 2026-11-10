"""Read actual starter and prepared-menu capacity without the onboarding flow."""
import logging
import time

from yt_playlist.rec import home_cards, home_prepared

MIN_MIXES = 4


def prepared_mixes_ready(store, now=None) -> bool:
    """Open Mix tapes with four playable offers; the first artist set is a starter."""
    now = time.time() if now is None else now
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    try:
        payload = home_prepared.peek(store, now, readiness_only=True)
        return bool(payload and len(home_prepared.cards(
            store, payload, now, service, record=False, readiness_only=True).protos) >= MIN_MIXES)
    except (ValueError, TypeError, KeyError):
        pass
    return False


def starter_album(store):
    """Choose a real release immediately, without models or provider requests.

    Saved albums need no materialized tracks: the existing album player obtains
    the complete release at play time. Otherwise prefer known releases by artists
    with several owned songs or listening days. An incidental track is never
    presented as a complete album tracklist.
    """
    from yt_playlist.library.album_fetch import is_upload_album

    def usable(album):
        browse = album.get('browse') or ''
        return (browse.startswith('MPRE') or is_upload_album(browse)) and bool(album.get('title'))

    with store.read_view() as reader:
        saved = [album for album in reader.collection.get_saved_albums() if usable(album)]
        if saved:
            album = next((album for album in saved if album.get('type') != 'Single'), saved[0])
            return {**album, 'saved': True, 'upload': is_upload_album(album['browse'])}
        # Count distinct owned recordings, not playlist occurrences or partial
        # imports. Catalog releases need not have been saved to be worth trying.
        album = next((row for row in reader.readiness.familiar_albums() if usable(row)), None)
    return {**album, 'saved': False, 'upload': is_upload_album(album['browse'])} if album else None
