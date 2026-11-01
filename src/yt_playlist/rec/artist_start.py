"""An empty account's explicit artist input and bounded YouTube catalog reads.

Provider identities are kept intact. Suggested artists never become seeds until
selected, and discovered songs never become library ownership or listening history.
"""
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock, BoundedSemaphore
import re
import time

from yt_playlist.core.accounts import catalog_client
from yt_playlist.core import sync_state
from yt_playlist.util.matching import identity_key
from yt_playlist.util.duration import parse_duration

MIN_ARTISTS = 3
MAX_ARTISTS = 12
_BROWSE_ID = re.compile(r'UC[A-Za-z0-9_-]{6,98}\Z')


class CatalogUnavailable(ValueError):
    pass


def thumbnail(rows):
    for row in reversed(rows or []):
        url = row.get('url', '')
        if url.startswith('https://'):
            return url
    return ''


def artist(row):
    browse_id = row.get('browseId') or row.get('channelId') or ''
    name = row.get('artist') or row.get('name') or row.get('title') or ''
    if not _BROWSE_ID.fullmatch(browse_id) or not isinstance(name, str) or not name.strip():
        return None
    return {'browse_id': browse_id, 'name': name.strip()[:160],
            'thumbnail': thumbnail(row.get('thumbnails'))}


def required(store):
    """Offer artist input when a connected library has no usable owned music.

    Unread playlists or albums retain their retry status. Choosing artists does
    not declare those resources empty or require a globally successful sync.
    """
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return required(reader)
    # Creating the first set also mirrors its tracks locally. Those tracks
    # remain generated output until adopted; saving them isn't a first listen.
    if store.conn.execute(
            'SELECT 1 FROM playable_library_tracks t WHERE '
            + store.rec_eligibility.track_length_clause('t.duration_s') + ' AND ('
            'NOT EXISTS(SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
            'JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id WHERE pt.track_id=t.id) '
            'OR EXISTS(SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
            'LEFT JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id '
            'WHERE pt.track_id=t.id AND q.playlist_scope_id IS NULL)) LIMIT 1').fetchone():
        return False
    if store.collection.get_saved_albums():
        return False
    # History and incidental extension-observed plays are not artist setup.
    # Graduate after explicit picks and a live start from their generated set.
    # A generated mirror, an imported play or an unrelated song cannot do this.
    if len(store.discovery.starter_artists()) >= MIN_ARTISTS:
        videos = [track['video_id'] for track in store.discovery.starter_tracks()]
        if videos and store.conn.execute(
                'SELECT 1 FROM play_events e JOIN play_event_sources s ON s.event_id=e.id '
                "WHERE s.source='live' AND e.is_generated=1 "
                f"AND e.video_id IN ({','.join('?' for _ in videos)}) LIMIT 1", videos).fetchone():
            return False
    if store.settings.get_setting('last_sync_at') is not None:
        return True
    # Wait for evidence that each configured account's library was reached.
    # Empty saved-album pages can fail parsing just like empty Liked Music.
    # Neither failure should strand an account with no playable collection on
    # First sounds. Offer explicit picks while the ordinary imports retry.
    state = sync_state.read(store.conn)
    accounts = {str(account.id) for account in store.accounts.get_accounts()}
    return bool(accounts) and all(
        state.get('accounts', {}).get(account, {}).get('resources', {})
            .get('playlist_listing', {}).get('status') == 'complete'
        for account in accounts)


def first_set(store, now):
    """Keep a usable first offer while ordinary background preparation catches up."""
    from yt_playlist.rec import home_cards
    choices = store.discovery.starter_artists()
    tracks = store.discovery.starter_tracks()
    if len(choices) < MIN_ARTISTS or not tracks:
        return None
    proto = home_cards._proto(store, 'artist_start', 'From your artist picks', tracks, now)
    proto['note'] = 'A first set from the artists you chose. More music is taking shape.'
    proto['recipe'] = {'model': 'artist_start', 'artists': [choice['browse_id'] for choice in choices]}
    proto['experiment_id'] = home_cards.experiment_id(proto)
    return proto


class ArtistCatalog:
    """Small coalesced cache. Slow catalog calls never hold the Store connection."""
    def __init__(self, clients):
        self.clients = clients
        self._cache = OrderedDict()
        self._pending = {}
        self._lock = Lock()
        self._slots = BoundedSemaphore(2)

    def _read(self, key, fetch):
        with self._lock:
            now = time.monotonic()
            saved = self._cache.get(key)
            if saved and saved[0] > now:
                self._cache.move_to_end(key)
                if isinstance(saved[1], Exception):
                    raise saved[1]
                return saved[1]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result(timeout=35)
        try:
            client = catalog_client(self.clients())
            if client is None:
                raise CatalogUnavailable('Open YouTube Music with the extension connected, then try again.')
            if not self._slots.acquire(timeout=5):
                raise CatalogUnavailable('Music search is busy. Try again in a moment.')
            try:
                result = fetch(client)
            finally:
                self._slots.release()
        except Exception as exc:
            error = exc if isinstance(exc, CatalogUnavailable) else CatalogUnavailable(
                'YouTube Music could not be reached. Your artist choices are saved. Try again.')
            with self._lock:
                self._cache[key] = (time.monotonic() + 10, error)
                self._cache.move_to_end(key)
                while len(self._cache) > 64:
                    self._cache.popitem(last=False)
                self._pending.pop(key, None)
                future.set_exception(error)
            raise error from exc
        with self._lock:
            self._cache[key] = (time.monotonic() + 900, result)
            self._cache.move_to_end(key)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            self._pending.pop(key, None)
            future.set_result(result)
        return result

    def search(self, query):
        query = query.strip()[:80]
        if len(query) < 2:
            return []
        rows = self._read(('search', query.casefold()),
                          lambda client: client.search(query, filter='artists', limit=8))
        result, seen = [], set()
        for row in rows or []:
            choice = artist(row)
            if choice and choice['browse_id'] not in seen:
                result.append(choice)
                seen.add(choice['browse_id'])
        return result[:8]

    def page(self, browse_id):
        if not _BROWSE_ID.fullmatch(browse_id):
            raise ValueError('Choose an artist from the search results.')
        return self._read(('artist', browse_id), lambda client: client.get_artist(browse_id))

    def related(self, store):
        groups = []
        for choice in store.discovery.starter_artists()[:2]:
            page = self.page(choice['browse_id'])
            group = []
            for row in ((page.get('related') or {}).get('results') or [])[:8]:
                suggestion = artist(row)
                if suggestion and suggestion['browse_id'] != choice['browse_id']:
                    group.append({**suggestion, 'because': choice['name']})
            groups.append(group)
        return [group[index] for index in range(8) for group in groups if len(group) > index]

    def select(self, store, browse_id, selected, now):
        existing = next((row for row in store.discovery.starter_artists()
                         if row['browse_id'] == browse_id), None)
        if not selected:
            if existing:
                store.discovery.select_starter_artist(existing['name'], browse_id, None, False, now)
            return []
        page = self.page(browse_id)
        choice = artist({**page, 'browseId': browse_id})
        if choice is None:
            raise CatalogUnavailable('This artist is unavailable. Try another search.')
        store.discovery.select_starter_artist(choice['name'], browse_id, choice['thumbnail'], True, now)
        for song in ((page.get('songs') or {}).get('results') or [])[:8]:
            vid, title = song.get('videoId'), song.get('title')
            if not vid or not title or song.get('isAvailable') is False:
                continue
            credits = song.get('artists') or [{'name': choice['name'], 'id': browse_id}]
            name = ', '.join(row.get('name', '') for row in credits if row.get('name')) or choice['name']
            store.discovery.upsert_discovered_track(
                identity_key(title, name), vid, title, name, (song.get('album') or {}).get('name') or '',
                thumbnail(song.get('thumbnails')), None, None, 'artist:' + browse_id, now,
                credits=credits,
                duration_s=song.get('duration_seconds') or parse_duration(song.get('duration')))
        result = []
        for row in ((page.get('related') or {}).get('results') or [])[:8]:
            related = artist(row)
            if related and related['browse_id'] != browse_id:
                result.append({**related, 'because': choice['name']})
        return result
