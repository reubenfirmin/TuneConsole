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
    with store.read_view() as reader:
        selected = select_starter_album(reader.collection.get_saved_albums(), saved=True)
        if selected:
            return selected
        # Count distinct owned recordings, not playlist occurrences or partial
        # imports. Catalog releases need not have been saved to be worth trying.
        return select_starter_album(reader.readiness.familiar_albums(), saved=False)


def select_starter_album(albums, *, saved):
    """Detached release selection; an incidental track never supplies a tracklist."""
    from yt_playlist.library.album_fetch import is_upload_album
    usable = [album for album in albums if bool(album.get('title')) and (
        (album.get('browse') or '').startswith('MPRE') or is_upload_album(album.get('browse') or ''))]
    if not usable:
        return None
    album = next((album for album in usable if album.get('type') != 'Single'), usable[0]) if saved else usable[0]
    return {**album, 'saved': saved, 'upload': is_upload_album(album['browse'])}
