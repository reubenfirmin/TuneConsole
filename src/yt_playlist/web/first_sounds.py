"""Continue the onboarding playlist without opening the later Radio destination."""
import json
import logging
import threading
import time
from uuid import uuid4

from yt_playlist.core.activity import check_stopping
from yt_playlist.core.accounts import client_for
from yt_playlist.rec import home_prepared, onboarding
from yt_playlist.rec.radio_candidates import eligible_seeds
from yt_playlist.web.radio_edits import AppendOperation, RadioEdits
from yt_playlist.web.radio_mutations import PlaylistTarget, RadioMutations
from yt_playlist.web.radio_persistence import operation_values
from yt_playlist.web.radio_playback import PlaybackRequest, RadioPlayback
from yt_playlist.web.radio_playlist_io import PendingWrite, PlaylistChanged, PlaylistEdit, RadioPlaylistIO
from yt_playlist.web.radio_playlist_tail import TailView

KEY = 'onboard_first_sounds'
LOOKAHEAD = 3
log = logging.getLogger(__name__)


def _saved_position(tracks, player, playing):
    # The sensor proves a native counterpart's saved occurrence. Borrow that
    # slot only while the independent observer reports the same actual upload.
    item = playing.get('queue_item')
    if (item and playing.get('video_id') == player.get('video_id')
            and playing.get('playlist_ytm_id') == player.get('playlist_ytm_id')
            and item.get('playlist') == player.get('playlist_ytm_id')):
        index = item.get('index')
        if index is not None:
            return index if (type(index) is int and 0 <= index < len(tracks)
                             and tracks[index]['video_id'] == item.get('video_id')) else -1
        matches = [i for i, track in enumerate(tracks) if track['video_id'] == item.get('video_id')]
        return matches[0] if len(matches) == 1 else -1
    return next((i for i, track in enumerate(tracks) if track['video_id'] == player.get('video_id')), -1)


class FirstSounds:
    def __init__(self, ctx):
        self.ctx = ctx
        self.operation = threading.Lock()
        self.next_check = 0
        self.edits = RadioEdits()
        self.playback = RadioPlayback(ctx.bridge)
        self.playlists = RadioPlaylistIO(ctx.store.playlists)
        self.mutations = RadioMutations(self.playlists, self, ctx.store.tracks, ctx.now_fn,
                                       paused=lambda: self.ctx.store.activity_paused)
        try:
            self.state = json.loads(ctx.store.settings.get_setting(KEY) or 'null')
        except ValueError:
            log.warning('Invalid First sounds checkpoint', exc_info=True)
            self.state = None

    def begin(self, result):
        """Adopt only the confirmed, locally retained result of a user's Play action."""
        if not result.get('pid') or not result.get('added'):
            return
        with self.operation:
            playlist = self.ctx.store.playlists.get_playlist(result['pid'])
            tracks = [{**track, 'key': track['identity_key']}
                      for track in self.ctx.store.charts.playlist_tracks_detail(result['pid'])]
            self.state = dict(episode_id=uuid4().hex, playlist=result['new_ytm'],
                local_id=result['pid'], identity=playlist.account_id, title=playlist.title,
                tracks=tracks, rejected=[], refresh=True, stopped=False,
                **operation_values(None))
            self.edits = RadioEdits()
            self._save()

    def _save(self):
        self.ctx.store.settings.set_setting(KEY, json.dumps(self.state, sort_keys=True))

    def record_edit(self, episode_id, operation):
        """Reuse the playlist transaction journal before sending or accepting a write."""
        if self.state['episode_id'] != episode_id:
            raise ValueError('First sounds operation belongs to another session')
        self.state.update(operation_values(operation))
        self._save()

    def prepare(self):
        """Heartbeats schedule bounded work; the bridge response pump never waits for I/O."""
        state, playing = self.state, self.playback.current()
        if (not state or state.get('stopped') or playing.get('playlist_ytm_id') != state['playlist']
                or self.ctx.store.activity_paused or not self.ctx.work.accepting
                or time.monotonic() < self.next_check or not self.operation.acquire(blocking=False)):
            return
        self.next_check = time.monotonic() + 2

        def maintain():
            try:
                self.maintain()
            except PlaylistChanged:
                log.warning('First sounds stopped after an external playlist edit', exc_info=True)
                self.state['stopped'] = True
                self._save()
            except Exception:
                log.debug('First sounds continuation deferred', exc_info=True)
            finally:
                self.operation.release()
        try:
            self.ctx.work.start_thread(maintain, name='first-sounds')
        except Exception:
            self.operation.release()
            raise

    def _candidates(self):
        """Use prepared suggestions when available, then newly acquired playable music."""
        store = self.ctx.store
        payload = home_prepared.peek(store, self.ctx.now_fn(), display_only=True) or {}
        tracks = [track for menu in payload.get('menus', []) for proto in menu.get('protos', [])
                  for track in proto.get('tracks', [])]
        tracks += onboarding.library_sample(store, n=64)
        return eligible_seeds(store, tracks, self.ctx.now_fn())

    def maintain(self):
        """Keep a short confirmed buffer, preserving native playback and uncertain writes."""
        state = self.state
        playing = self.playback.current()
        if (not state or state.get('stopped') or self.ctx.store.activity_paused
                or playing.get('playlist_ytm_id') != state['playlist']):
            return
        tracks = state['tracks']
        observation = self.playback.observe(PlaybackRequest(state['playlist'],
            tuple(track['video_id'] for track in tracks), refresh=state.get('refresh', False)))
        player = observation.player(state['playlist'])
        if not player:
            return
        # Native queue refresh is asynchronous. Keep requesting its bounded
        # reconciliation until Music confirms the saved future is loaded.
        refresh = not observation.state.get('playback_synced', False)
        if refresh != state.get('refresh'):
            state['refresh'] = refresh
            self._save()
        position = _saved_position(tracks, player, self.playback.current())
        if position < 0:
            return
        _, client = client_for(self.ctx.store, self.ctx.client_provider(), account_id=state['identity'],
                               playlist_id=state['local_id'])
        target = PlaylistTarget(client, state['playlist'], state['local_id'], state['title'],
                                state['episode_id'], threading.Event(), state['identity'])
        edits = self.edits
        if state.get('pending') and edits.active is None:
            operation = AppendOperation(state['pending'])
            if state.get('pending_sent'):
                after = tuple(track['video_id'] for track in tracks) + (operation.track['video_id'],)
                operation.write = PendingWrite(PlaylistEdit('add', after, track=operation.track),
                    state.get('pending_started_at'), state.get('pending_result'))
            edits.begin(operation)
        candidates = None
        for _ in range(LOOKAHEAD):
            check_stopping()
            if (self.ctx.store.activity_paused
                    or self.playback.current().get('playlist_ytm_id') != state['playlist']):
                return
            if not edits.append:
                if len(tracks) - position - 1 >= LOOKAHEAD:
                    break
                candidates = self._candidates() if candidates is None else candidates
                excluded = {track['video_id'] for track in tracks} | set(state['rejected'])
                track = next((track for track in candidates if track['video_id'] not in excluded), None)
                if not track:
                    break
                edits.begin(AppendOperation(track))
                self.record_edit(state['episode_id'], edits.append)
            view = TailView(tuple(tracks), position, position, frozenset(state['rejected']),
                            {track['video_id']: track for track in tracks})
            result = self.mutations.append(target, edits, view)
            if result.status == 'waiting':
                return
            if result.status == 'confirmed':
                tracks = state['tracks'] = list(result.tracks)
                state['refresh'] = True
            elif result.rejected:
                state['rejected'].append(result.rejected['video_id'])
            edits.finish(edits.append)
            state.update(operation_values(None))
            self._save()
        # Native refresh retains the current song and position; it supplies no
        # fabricated playback items and never navigates or issues Play.
        if state.get('refresh'):
            observation = self.playback.observe(PlaybackRequest(state['playlist'],
                tuple(track['video_id'] for track in tracks), refresh=True))
            if observation.state.get('playback_synced'):
                state['refresh'] = False
                self._save()

