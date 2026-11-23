"""Account-scoped catalog search and confirmed song saves, independent of ratings."""
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
import json
from threading import BoundedSemaphore, Lock
import time
from urllib.parse import quote, urlencode

from yt_playlist.util.action_kinds import SAVE_SONG
from yt_playlist.util.music_catalog import VIDEO_ID, artist, song, thumbnail
from yt_playlist.util.music_matching import relevance, words
from yt_playlist.util.navigation import album_url, artist_url


class MusicSearch:
    def __init__(self, access, tracks, actions, *, now, admit, logger, withdraw=None, entities=None, collection=None):
        self.access, self.tracks, self.actions = access, tracks, actions
        self.now, self.admit, self.logger = now, admit, logger
        self.withdraw = withdraw
        self.entities = entities
        self.collection = collection
        self._cache, self._pending = OrderedDict(), {}
        self._lock, self._writes, self._slots = Lock(), Lock(), BoundedSemaphore(2)

    @contextmanager
    def _write_slot(self):
        if not self._writes.acquire(timeout=3):
            raise TimeoutError('Song save already in progress')
        try:
            yield
        finally:
            self._writes.release()

    def _search(self, account, client, query, *, fresh=False, result_filter='songs'):
        key = (account, id(client), query, result_filter)
        with self._lock:
            saved = self._cache.get(key)
            if not fresh and saved and saved[0] > time.monotonic():
                self._cache.move_to_end(key)
                return saved[1]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result(timeout=35)
        try:
            if not self._slots.acquire(timeout=3):
                raise TimeoutError('Catalog search capacity reached')
            try:
                rows = client.search(query, filter=result_filter, limit=40, ignore_spelling=True)
                if not isinstance(rows, list):
                    raise ValueError('Incomplete catalog search')
                # Recover a title/artist split when the provider treats a mixed
                # query as a title. Only credited artists in these results qualify.
                terms = set(words(query))
                artists = {artist.get('name', '') for row in rows for artist in row.get('artists') or []}
                candidates = [name for name in artists if set(words(name)) and set(words(name)) < terms]
                if result_filter == 'songs' and candidates:
                    artist = sorted(candidates, key=lambda name: (-len(words(name)), name.casefold()))[0]
                    artist_terms = set(words(artist))
                    has_original = any(relevance(query, row.get('title'), artist)[0] == len(terms)
                        and any(credit.get('name') == artist for credit in row.get('artists') or []) for row in rows)
                    if not has_original:
                        title = ' '.join(term for term in words(query) if term not in artist_terms)
                        try:
                            extra = client.search(f'"{title}" {artist}', filter='songs', limit=40, ignore_spelling=True)
                            if isinstance(extra, list):
                                rows += extra
                        except Exception:
                            self.logger.debug('Refined song search unavailable; retaining initial results', exc_info=True)
            finally:
                self._slots.release()
        except Exception as exc:
            with self._lock:
                self._pending.pop(key, None)
                future.set_exception(exc)
            raise
        with self._lock:
            self._cache[key] = (time.monotonic() + 60, rows)
            self._cache.move_to_end(key)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            self._pending.pop(key, None)
            future.set_result(rows)
        return rows

    def _pending_saves(self, account):
        for action in self.actions.get_pending_actions(SAVE_SONG):
            params = json.loads(action.params_json)
            if params['account_id'] == account:
                yield action.id, params

    def search(self, query):
        query = query.strip()[:80]
        if len(query) < 2:
            return []
        account, client = self.access.resolve()
        rows = self._search(account, client, query)
        pending = {params['song']['video_id']: params.get('saved', True)
                   for _, params in self._pending_saves(account)}
        results, seen = [], set()
        for raw in rows:
            item = song(raw)
            if item is None or item['video_id'] in seen:
                continue
            seen.add(item['video_id'])
            membership = self.tracks.song_library_membership(item['video_id'])
            item['owned'] = membership['owned']
            item['library_saved'] = raw.get('inLibrary')
            item['can_add'] = bool(raw.get('inLibrary') or (raw.get('feedbackTokens') or {}).get('add'))
            item['can_remove'] = bool((raw.get('feedbackTokens') or {}).get('remove')) and not membership['retained']
            if item['video_id'] in pending:
                item.update(action_state='pending', pending_saved=pending[item['video_id']])
            results.append(item)
        return sorted(results, key=lambda item: relevance(query, item['title'], item['artist']), reverse=True)[:20]

    def sections(self, query):
        """One native mixed search supplies albums, artists and playlists."""
        result = {'artists': [], 'albums': [], 'playlists': []}
        query = query.strip()[:80]
        if len(query) < 2:
            return result
        account, client = self.access.resolve()
        pending = self.entities.pending(account) if self.entities else {}
        seen = set()
        for raw in self._search(account, client, query, result_filter=None):
            kind = raw.get('resultType')
            if kind == 'artist':
                item = artist(raw)
                if not item:
                    continue
                browse = item['browse_id']
                item['title'] = item['name']
                item['href'] = artist_url(browse=browse)
            elif kind in ('album', 'playlist'):
                browse = raw.get('browseId')
                title = raw.get('title')
                if not isinstance(browse, str) or not VIDEO_ID.fullmatch(browse) or not isinstance(title, str) or not title.strip():
                    continue
                if kind == 'playlist':
                    browse = 'VL' + browse.removeprefix('VL')
                credits = ', '.join(row['name'] for row in raw.get('artists') or [] if row.get('name'))
                author = raw.get('artist') or raw.get('author') or ''
                if isinstance(author, dict):
                    author = author.get('name', '')
                item = dict(browse_id=browse, title=title.strip()[:240], thumbnail=thumbnail(raw.get('thumbnails')),
                            sub=' · '.join(str(value) for value in (credits or author, raw.get('year')) if value))
                item['href'] = album_url(browse) if kind == 'album' else (
                    'https://music.youtube.com/playlist?' + urlencode({'list': browse.removeprefix('VL')}))
            else:
                continue
            key = kind, browse
            if key in seen or len(result[kind + 's']) >= 8:
                continue
            seen.add(key)
            item.update(kind=kind, can_add=True, can_remove=False,
                        library_saved=raw.get('subscribed') if kind == 'artist' else raw.get('inLibrary'))
            if key in pending:
                item['action_state'] = 'pending'
            playlist = raw.get('shuffleId') if kind == 'artist' else raw.get('playlistId')
            if kind == 'playlist':
                playlist = playlist or browse.removeprefix('VL')
            item['play_url'] = ('https://music.youtube.com/playlist?' + urlencode({'list': playlist}) if playlist else
                                'https://music.youtube.com/browse/' + quote(browse, safe=''))
            result[kind + 's'].append(item)
        if self.collection:
            saved_albums = self.collection.saved_album_ids(browse_ids=[item['browse_id'] for item in result['albums']])
            for item in result['albums']:
                item['owned'] = item['browse_id'] in saved_albums
        return result

    def save_entity(self, kind, browse, *, check=False):
        result = self.entities.save(kind, browse, check=check)
        if result['state'] == 'saved':
            with self._lock:
                self._cache.clear()
        return result

    def save(self, video, query, *, onboarding=False, check=False, saved=True):
        if not VIDEO_ID.fullmatch(video):
            raise ValueError('Invalid song identity')
        query = query.strip()[:80]
        account, client = self.access.resolve()
        # Serialize intent creation and provider writes, without a database lock.
        # An unknown outcome is reconciled on subsequent requests, never replayed.
        with self._write_slot():
            prior = next(((action_id, params) for action_id, params in self._pending_saves(account)
                          if params['song']['video_id'] == video), None)
            if prior:
                action_id, params = prior
                query, onboarding = params['query'], params['onboarding']
                saved = params.get('saved', True)
            state = 'saved' if saved else 'removed'

            def result():
                membership = self.tracks.song_library_membership(video)
                return {'video_id': video, 'state': 'saved' if membership['owned'] else state,
                        'owned': membership['owned'], 'library_saved': saved,
                        'can_remove': bool((raw.get('feedbackTokens') or {}).get('remove')) and not membership['retained']}

            def apply():
                if saved:
                    self.admit(item, onboarding)
                elif self.withdraw:
                    self.withdraw(item)

            def confirm(action_id):
                # Publish local ownership and its acknowledgement together with
                # respect to a native snapshot finishing on another thread.
                with self.actions.guard():
                    apply()
                    self.actions.update_action(action_id, 'executed', self.now())

            rows = self._search(account, client, query, fresh=True)
            raw = next((row for row in rows if row.get('videoId') == video), None)
            # A stale or forged control cannot remove a song retained by another
            # collection. Pending writes still need their original reconciliation.
            if not saved and not prior and self.tracks.song_library_membership(video)['retained']:
                return {'video_id': video, 'state': 'saved', 'owned': True, 'can_remove': False}
            item = song(raw) if raw else None
            if not item:
                return {'video_id': video, 'state': 'pending' if prior else 'unavailable'}
            if raw.get('inLibrary') is saved:
                if prior or not saved:
                    if not prior:
                        params = dict(account_id=account, query=query, song=item, onboarding=onboarding, saved=saved)
                        action_id = self.actions.record_action(SAVE_SONG, json.dumps(params), '{}', 'pending', '{}', self.now())
                    confirm(action_id)
                else:
                    apply()
                return result()
            if prior:
                return {'video_id': video, 'state': 'pending'}
            if check:
                return {'video_id': video, 'state': 'retry'}
            token = (raw.get('feedbackTokens') or {}).get('add' if saved else 'remove')
            if not token:
                return {'video_id': video, 'state': 'unavailable'}
            params = dict(account_id=account, query=query, song=item, onboarding=onboarding, saved=saved)
            action_id = self.actions.record_action(SAVE_SONG, json.dumps(params), '{}', 'pending', '{}', self.now())
            try:
                response = client.edit_song_library_status([token])
                processed = (response or {}).get('feedbackResponses') or []
                confirmed = bool(processed) and all(row.get('isProcessed') is True for row in processed)
                if not confirmed:
                    # A response can omit feedback acknowledgement. Only a fresh,
                    # account-scoped observation establishes success in that case.
                    confirmed = any(row.get('videoId') == video and row.get('inLibrary') is saved
                                    for row in self._search(account, client, query, fresh=True))
                if confirmed:
                    confirm(action_id)
                    with self._lock:
                        self._cache.clear()
                    return result()
            except Exception as exc:
                if getattr(exc, 'outcome', None) == 'not-dispatched':
                    self.actions.update_action(action_id, 'failed', self.now())
                    return {'video_id': video, 'state': 'retry'}
                self.logger.warning('Song library save awaits confirmation: %s', video, exc_info=True)
            return {'video_id': video, 'state': 'pending'}
