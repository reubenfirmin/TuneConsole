"""Bounded artist photography, prepared independently of TV playback."""
from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded, WorkClosing
from yt_playlist.providers import wikipedia
from yt_playlist.util import net
from yt_playlist.util.matching import search_squash
from yt_playlist.util.thumbnails import best_thumb

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
    requests = store.rec_proposals.get_proposals(REQUESTS) or {}
    if key not in requests:
        requests[key] = name
        store.rec_proposals.put_proposals(REQUESTS, dict(list(requests.items())[-LIMIT:]), now)
    return True


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
    for note in (store.rec_proposals.get_proposals('album_notes') or {}).values():
        art = note.get('artwork') or {}
        if search_squash(note.get('artist') or '') == key and art.get('kind') == 'artist' and art.get('url'):
            photos.append(art['url'])
    cached = (store.rec_proposals.get_proposals(SURFACE) or {}).get(key, {})
    photos.extend(cached.get('images', []))
    return list(dict.fromkeys(photos))[:6], cached


def prepare(store, client, names, now):
    """One artist per slice; publish the playable pool before optional image I/O."""
    cache = store.rec_proposals.get_proposals(SURFACE) or {}
    requests = store.rec_proposals.get_proposals(REQUESTS) or {}
    pending = [name for name in dict.fromkeys([*requests.values(), *names])
               if now >= cache.get(search_squash(name), {}).get('retry_at', 0)]
    if not pending:
        return False
    name = pending[0]
    photos, _ = images(store, name)
    failed = False
    try:
        check_cancellable_work()
        for artist in (client.search(name, filter='artists') or [])[:5]:
            if (search_squash(artist.get('artist') or artist.get('title') or '') == search_squash(name)
                    and artist.get('resultType') in (None, 'artist')):
                if image := best_thumb(artist.get('thumbnails')):
                    photos.append(image)
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        failed = True
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
    cache.pop(search_squash(name), None)
    cache[search_squash(name)] = {'images': list(dict.fromkeys(photos))[:6],
        'retry_at': max(now + (60 if failed else 86400), vendor_retry_at), 'at': now}
    store.rec_proposals.put_proposals(SURFACE, dict(list(cache.items())[-LIMIT:]), now)
    if not failed and search_squash(name) in requests:
        requests.pop(search_squash(name))
        store.rec_proposals.put_proposals(REQUESTS, requests, now)
    return len(pending) > 1
