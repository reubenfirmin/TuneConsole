"""Cached sleeve notes; the existing Home worker resolves one release per pass.

Raw public provider evidence lives in immutable JSON artifacts. SQLite retains a
bounded display projection and a coalesced queue, with no schema changes.
"""
import logging

from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded, check_cancellable_work
from yt_playlist.providers import release_notes, album_editorial
from yt_playlist.rec import album_descriptions
from yt_playlist.providers.discogs import _artist_key
from yt_playlist.library.album_fetch import fetch_album
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.util.matching import recording_title
from yt_playlist.util.thumbnails import best_thumb

_LOG = logging.getLogger(__name__)


def read(store, album):
    note = _release_only((store.rec_proposals.get_proposals('album_notes') or {}).get(album['browse_id'], {}))
    note['description'] = album_descriptions.read(store, album) or note.get('description')
    return note


def _release_only(note):
    note = {key: value for key, value in note.items()
            if key not in {'artist_note', 'artist_url', 'artist_revision'}}
    def reader_source(item):
        url = (item or {}).get('url', '').casefold()
        return release_notes.public_url(url) and not any(
            host in url for host in ('wikipedia.org', 'musicbrainz.org'))
    for key in ('description', 'review', 'rating'):
        if note.get(key) and not reader_source(note[key]):
            note[key] = None
    # Old hand-written research must not mask a gap in automatic acquisition.
    for key in ('description', 'review'):
        if note.get(key) and (note[key].get('acquisition') != 'automatic'
                              or note[key].get('kind') == 'catalog'):
            note[key] = None
    note['links'] = [link for link in note.get('links') or [] if reader_source(link)]
    artwork = note.get('artwork') or {}
    if (artwork.get('kind') not in {'artist', 'performance', 'location', 'genre'}
            or not release_notes.public_url(artwork.get('url'))
            or not release_notes.public_url(artwork.get('source_url'))):
        note['artwork'] = None
    return note


def _artist_artwork(client, album):
    """Artist-only search results; an album or song thumbnail is never a backdrop."""
    if client is None or album['artist'].casefold() in {'various', 'various artists'}:
        return None
    entry = album_editorial.lookup(album) or {}
    names = {album['artist'], entry.get('artist', ''), *entry.get('artist_aliases', [])} - {''}
    keys = {_artist_key(name) for name in names}
    check_cancellable_work()
    for row in (client.search(album['artist'], filter='artists') or [])[:5]:
        if (_artist_key(row.get('artist') or '') not in keys
                or row.get('resultType') not in (None, 'artist')):
            continue
        url = release_notes.public_url(best_thumb(row.get('thumbnails')))
        browse = row.get('browseId') or ''
        if url and browse.startswith('UC') and url != album.get('thumbnail'):
            return {'url': url, 'source_url': 'https://music.youtube.com/channel/' + browse,
                    'credit': album['artist'] + ' · artist photography', 'kind': 'artist'}
    return None


def _catalog_metadata(client, album, store):
    """Complete tracklists inform familiarity; catalogue facts never become filler prose."""
    if client is None:
        return None
    check_cancellable_work()
    release = fetch_album(client, album['browse_id'])
    if (recording_title(release.get('title')) != recording_title(album['title'])
            or _artist_key(album['artist']) not in {
                _artist_key(artist.get('name')) for artist in release.get('artists') or []}):
        return None
    tracks = release.get('tracks') or []
    if (not tracks or any(not track.get('title') for track in tracks)
            or release.get('continuations') or release.get('continuation')):
        return None
    count = release.get('trackCount')
    if count is not None and str(count).replace(',', '') != str(len(tracks)):
        return None
    remember_album(store, album['browse_id'], release)
    facts = {key: release.get(key) for key in ('title', 'artists', 'year', 'trackCount')}
    facts['tracks'] = [{key: track.get(key) for key in ('title', 'duration')} for track in tracks]
    return {'provider': 'youtube_album', 'response': facts}


def prepare(store, now, albums=(), *, client=None, now_fn=None):
    """Keep menu publication ahead of optional network work, with bounded retries."""
    # A refresh can leave the previous set queued. Reconcile against the
    # worker's current menu before spending on public lookups or paid copy.
    store.rec_proposals.request_album_notes(albums, now, current_only=True)
    queued = store.rec_proposals.get_proposals('album_note_requests') or {}
    if not queued:
        return False
    album = next(iter(queued.values()))
    browse = album['browse_id']
    cached = _release_only((store.rec_proposals.get_proposals('album_notes') or {}).get(browse, {}))
    if cached.get('editorial_version') == 6 and cached.get('retry_at', 0) > now:
        store.rec_proposals.publish_album_note(browse, cached, now)
        return len(queued) > 1
    result = release_notes.fetch(album, store)
    description, generation = album_descriptions.get(store, album, now, now_fn=now_fn)
    result['evidence'].append({'provider': 'deepseek', 'response': generation})
    if description:
        result['description'] = description
    elif cached.get('description'):
        result['description'] = cached['description']
    # Completion is useful for the listening indicator, independently of prose.
    summary = store.collection.album_listening_summary([browse])[browse]
    if client is not None and not summary['complete']:
        try:
            evidence = _catalog_metadata(client, album, store)
            if evidence:
                result['evidence'].append(evidence)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception:
            result['retry'] = True
            result['evidence'].append({'provider': 'youtube_album', 'status': 'unavailable'})
            _LOG.debug('Album metadata deferred for %s', browse, exc_info=True)
    editorial = album_editorial.lookup(album)
    result['artwork'] = (editorial or {}).get('artwork') or cached.get('artwork')
    if not result['artwork']:
        try:
            result['artwork'] = _artist_artwork(client, album)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception:
            result['retry'] = True
            _LOG.debug('Album artwork deferred for %s', browse, exc_info=True)
    if result['artwork']:
        result['evidence'].append({'provider': 'album_artwork', 'response': result['artwork']})
    raw = {'kind': 'album_editorial', 'album': album, 'at': now,
           'sources': result.pop('evidence')}
    retry = result.pop('retry')
    # Retain last-good facts through provider outages. A confirmed miss has a
    # seven-day negative cache; transient failures retry after an hour.
    retry_at = now + (3600 if retry else 7 * 86400)
    if not description and generation.get('retry_at', 0) > now:
        retry_at = min(retry_at, generation['retry_at'])
    note = _release_only({**cached, **result, 'editorial_version': 6, 'at': now,
                         'retry_at': retry_at})
    fields = ('rating', 'label', 'track_count', 'links', 'description', 'review', 'artwork')
    retained = False
    if retry:
        for key in fields:
            retained |= bool(not result.get(key) and cached.get(key))
            note[key] = result.get(key) or cached.get(key)
    # Unchanged facts reuse their provenance. Repeated outages must not create
    # an indefinitely retained chain of identical failure artifacts.
    unchanged = cached.get('evidence') and all(note.get(key) == cached.get(key) for key in fields)
    if not unchanged:
        if retained and cached.get('evidence'):
            raw['previous'] = {'$artifact': cached['evidence']}
        try:
            note['evidence'] = store.rec_ledger.evidence().artifact(raw)
        except (OSError, ValueError):
            note['evidence'] = cached.get('evidence')
            _LOG.warning('Album editorial evidence unavailable', exc_info=True)
    store.rec_proposals.publish_album_note(browse, note, now)
    return len(queued) > 1
