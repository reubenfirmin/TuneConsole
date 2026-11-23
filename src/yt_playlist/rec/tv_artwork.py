"""Bounded artist photography, prepared independently of TV playback."""
from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.providers import wikipedia
from yt_playlist.util import net
from yt_playlist.util.matching import search_squash

SURFACE = 'tv_artist_artwork'
LIMIT = 60
REQUESTS = 'tv_artist_artwork_requests'


def request(store, name, now):
    """Include a saved video's known artist even after its video leaves the pool."""
    if getattr(store, 'activity_paused', False):
        return False
    if store.artists.resolve(name) is None:
        return False
    key = search_squash(name)
    return store.tv.request_artwork(key, name, now)


def images(store, name):
    key = search_squash(name)
    photos = []
    aid = store.artists.resolve(name)
    if aid is not None:
        _, artists = store.discovery.present_home_tiles([], [aid])
        photos.extend(a['thumbnail'] for a in artists.values() if a.get('thumbnail'))
        summary = store.wiki.get(f'artist:{store.artists.key(aid)}')
        if summary and summary.get('found') and summary.get('thumbnail'):
            photos.append(summary['thumbnail'])
    notes = store.memoized_read('tv-artwork-notes',
        lambda: store.rec_proposals.get_proposals('album_notes') or {})
    for note in notes.values():
        art = note.get('artwork') or {}
        if search_squash(note.get('artist') or '') == key and art.get('kind') == 'artist' and art.get('url'):
            photos.append(art['url'])
    artwork = store.memoized_read('tv-artwork-images',
        lambda: store.rec_proposals.get_proposals(SURFACE) or {})
    cached = artwork.get(key, {})
    photos.extend(cached.get('images', []))
    return list(dict.fromkeys(photos))[:6], cached


def prepare(store, client, names, now):
    """One artist per slice; publish the playable pool before optional image I/O."""
    from yt_playlist.controllers.tv_artwork import TVArtwork
    return TVArtwork(store.rec_proposals, store.tv, images=lambda name: images(store, name),
        search=lambda name: _search(client, name), vendor=lambda name, at: _vendor(store, name, at)).prepare(names, now)


def _search(client, name):
    try:
        check_cancellable_work()
        return YouTubeMusicAccess.for_client(client).artist_artwork(name), False
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        return [], True


def _vendor(store, name, now):
    photos, failed = [], False
    vendor_retry_at = 0
    with wikipedia.access(store), net.probe_errors() as errors:
        try:
            check_cancellable_work()
            if image := wikipedia.fetch_artwork(name):
                photos.append(image['url'])
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as exc:
            failed = True
            vendor_retry_at = getattr(exc, 'retry_at', 0)
    failed = failed or bool(errors)
    vendor_retry_at = max([vendor_retry_at, *(net.retry_after(error, now) for error in errors)])
    return photos, failed, vendor_retry_at
