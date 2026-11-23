"""An empty account's explicit artist input and bounded YouTube catalog reads.

Provider identities are kept intact. Suggested artists never become seeds until
selected, and discovered songs never become library ownership or listening history.
"""
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock, BoundedSemaphore
import time

from yt_playlist.core.accounts import catalog_client
from yt_playlist.rec import artist_model
from yt_playlist.util.matching import identity_key
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.music_catalog import ARTIST_ID as _BROWSE_ID, artist, thumbnail

MIN_ARTISTS = 3
MAX_ARTISTS = 12
DRAFT = 'artist_start_music_draft'


class CatalogUnavailable(ValueError):
    pass


def required(store):
    """Offer artist input when a connected library has no usable owned music.

    Unread playlists or albums retain their retry status. Choosing artists does
    not declare those resources empty or require a globally successful sync.
    """
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return required(reader)
    if store.settings.get_setting(DRAFT) == '1':
        # Completion follows the recorded choices even if an upload later
        # becomes unavailable. Playable rows still govern the first-set offer.
        videos = [*store.discovery.starter_song_videos(),
                  *(row['video_id'] for row in store.discovery.starter_tracks())]
        return not bool(videos and store.readiness.live_starter_played(videos))
    # Creating the first set also mirrors its tracks locally. Those tracks
    # remain generated output until adopted; saving them isn't a first listen.
    owned = store.readiness.playable_owned(exclude_generated=True)
    if owned:
        return requires_artists(owned=owned)
    albums = bool(store.collection.get_saved_albums())
    if albums:
        return requires_artists(albums=albums)
    # History and incidental extension-observed plays are not artist setup.
    # Graduate after explicit picks and a live start from their generated set.
    # A generated mirror, an imported play or an unrelated song cannot do this.
    picks = len(store.discovery.starter_artists())
    played = False
    if picks >= MIN_ARTISTS:
        videos = [track['video_id'] for track in store.discovery.starter_tracks()]
        played = store.readiness.live_starter_played(videos)
        if played:
            return requires_artists(picks=picks, played=played)
    if store.settings.get_setting('last_sync_at') is not None:
        return requires_artists(imported=True)
    # Wait for evidence that each configured account's library was reached.
    # Empty saved-album pages can fail parsing just like empty Liked Music.
    # Neither failure should strand an account with no playable collection on
    # First sounds. Offer explicit picks while the ordinary imports retry.
    state = store.sync.state()
    accounts = {str(account.id) for account in store.accounts.get_accounts()}
    reached = bool(accounts) and all(
        state.get('accounts', {}).get(account, {}).get('resources', {})
            .get('playlist_listing', {}).get('status') == 'complete'
        for account in accounts)
    return requires_artists(reached=reached)


def requires_artists(*, owned=False, albums=False, picks=0, played=False, imported=False, reached=False):
    """Detached completion rule: generated/imported evidence cannot invent a live start."""
    return not (owned or albums or picks >= MIN_ARTISTS and played) and bool(imported or reached)


def first_set(store, now):
    """Keep a usable first offer while ordinary background preparation catches up."""
    from yt_playlist.rec import home_cards
    choices = store.discovery.starter_artists()
    tracks = starter_tracks(store)
    if not (saved_songs(store) or len(choices) >= MIN_ARTISTS) or not tracks:
        return None
    proto = home_cards._proto(store, 'artist_start', 'From your picks', tracks, now)
    proto['note'] = 'A first set from your songs and artists. More music is taking shape.'
    proto['recipe'] = {'model': 'artist_start', 'artists': [choice['browse_id'] for choice in choices],
                       'songs': [row['video_id'] for row in saved_songs(store)]}
    proto['experiment_id'] = home_cards.experiment_id(proto)
    return proto


def remember_song(store, video_id, now):
    store.discovery.select_starter_song(video_id, now)


def saved_songs(store):
    return store.discovery.starter_songs()


def starter_tracks(store):
    songs = saved_songs(store)
    seen = {row['video_id'] for row in songs}
    return [*songs, *(row for row in store.discovery.starter_tracks() if row['video_id'] not in seen)]


class ArtistCatalog:
    """Small coalesced cache. Slow catalog calls never hold the Store connection.

    Owns the First sounds artist-start commands/queries end to end: selection
    persistence (with the one worker refresh it requires), starter readiness,
    and the listen fragment's starter-set context.
    """
    def __init__(self, clients, *, trigger_refresh=None):
        self.clients = clients
        self.trigger_refresh = trigger_refresh
        self._cache = OrderedDict()
        self._pending = {}
        self._lock = Lock()
        self._slots = BoundedSemaphore(2)

    def state(self, store):
        choices = store.discovery.starter_artists()
        songs = starter_tracks(store)
        saved = saved_songs(store)
        return {'selected': choices, 'songs': len(songs), 'saved_songs': saved, 'minimum': MIN_ARTISTS,
                'complete': not required(store),
                'ready': bool(saved or len(choices) >= MIN_ARTISTS and songs)}

    def listen_context(self, store, now=None):
        from yt_playlist.rec import home_cards
        tracks = starter_tracks(store)
        choices = store.discovery.starter_artists()
        ready = bool(saved_songs(store) or len(choices) >= MIN_ARTISTS and tracks)
        return {'tracks': tracks, 'choices': choices,
                'ready': ready,
                'proto': home_cards._proto(store, 'artist_start', 'My first set', tracks, now)
                         if ready and now is not None else None}

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
        choices = store.discovery.starter_artists()
        excluded = {choice['browse_id'] for choice in choices}
        groups = []
        for choice in choices[:2]:
            try:
                page = self.page(choice['browse_id'])
            except (CatalogUnavailable, TimeoutError):
                page = None
            groups.append(self._related_choices(store, choice, page, excluded))
        result, seen = [], set(excluded)
        for index in range(8):
            for group in groups:
                if index < len(group) and group[index]['browse_id'] not in seen:
                    result.append(group[index])
                    seen.add(group[index]['browse_id'])
        return result

    def _related_choices(self, store, choice, page, excluded):
        browse = choice['browse_id']
        neighbors = artist_model.ranked_artist_neighbors(store,
            [artist_model.youtube_artist_key(store.artists, browse)],
            youtube=artist_model.youtube_neighbors(page),
            exclude=[artist_model.youtube_artist_key(store.artists, item) for item in excluded], topn=50)
        result = []
        for neighbor in neighbors:
            suggestion = artist_model.neighbor_reference(store.artists, neighbor)
            if suggestion and suggestion['browse_id'] and suggestion['browse_id'] not in excluded:
                result.append({**suggestion, 'because': choice['name'], 'sources': neighbor.sources})
                if len(result) == 8:
                    break
        return result

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
        result = self._related_choices(store, choice, page,
            {row['browse_id'] for row in store.discovery.starter_artists()})
        if self.trigger_refresh:
            self.trigger_refresh()
        return result

    def choose(self, store, browse_id, selected, now):
        related = self.select(store, browse_id, selected, now)
        return {**self.state(store), 'related': related}
