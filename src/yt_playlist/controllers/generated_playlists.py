"""Shared generated-playlist command and read-only recovery boundary."""
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.util.navigation import playlist_url


def reconciled_additions(recovery, tracks, present):
    """Presence confirms additions; absence never authorizes another write."""
    confirmed = set(recovery['confirmed']) | present
    rejected = set(recovery['rejected']) - present
    unattempted = set(recovery['unattempted']) - present
    videos = list(dict.fromkeys(track['video_id'] for track in tracks))
    return {**recovery, 'confirmed': [v for v in videos if v in confirmed],
            'rejected': [v for v in videos if v in rejected],
            'unattempted': [v for v in videos if v in unattempted],
            'uncertain': [v for v in videos if v not in confirmed | rejected | unattempted]}


class MixSaveIncomplete(RuntimeError):
    """The provider returned a destination before the Home launch failed."""

    def __init__(self, account_id, playlist, tracks):
        super().__init__('Mix launch failed after creating its destination')
        self.tracks = tracks
        self.recovery = {'account_id': account_id, 'playlist': playlist, 'confirmed': [],
                         'rejected': [], 'unattempted': [],
                         'uncertain': list(dict.fromkeys(track['video_id'] for track in tracks))}


class GeneratedPlaylists:
    def __init__(self, access, jobs, modes, *, create, now, measure, respond, enrich,
                 navigate, refresh, query, recipes=None, begin_intent=lambda: None, mix_playback=None):
        self.access, self.jobs, self.modes = access, jobs, modes
        self.create_destination, self.now, self.measure = create, now, measure
        self.respond, self.enrich, self.navigate, self.refresh = respond, enrich, navigate, refresh
        self.query_destination = query
        self.recipes = recipes
        self.begin_intent = begin_intent
        self.mix_playback = mix_playback

    def admit(self):
        """Reserve navigation at request arrival, before scheduling provider work."""
        return self.begin_intent()

    def create(self, title, tracks, *, recipe=None, account_id=None, client=None,
               intent=None, navigate=False, canvas=None, **options):
        if client is None:
            account_id, client = self.access.resolve(account_id=account_id)
        try:
            result = self.create_destination(title, tracks, client, self.now(), account_id,
                                             recipe=recipe, **options)
        except PlaylistAddUncertain as failure:
            failure.account_id = account_id
            self._confirmed_effects(dict(pid=getattr(failure, 'local_playlist_id', None),
                new_ytm=failure.playlist_id, added=len(failure.confirmed)), recipe)
            raise
        pid = result.get('pid')
        if pid is None:
            if self.jobs is not None:
                receipt = self.jobs.create(kind='generated_playlist')
                receipt.parameters = {'account_id': account_id, 'ytm': result['new_ytm']}
                receipt.result, receipt.done = {**result, 'recipe': recipe}, True
        url = playlist_url(pid, account_id=account_id, provider_id=result['new_ytm'])
        self._confirmed_effects(result, recipe)
        if navigate and result.get('added'):
            self.navigate(client, result['new_ytm'], intent)
        if result.get('added') and canvas and self.recipes is not None:
            self.recipes.set_cluster_canvas(result['new_ytm'], canvas, self.now())
        return {**result, 'playlist_url': url, 'status': 'confirmed' if result.get('added') else 'rejected'}

    def _confirmed_effects(self, result, recipe):
        pid = result.get('pid')
        if not pid or not result.get('added'):
            return
        now = self.now()
        receipt = f"generate:{result['new_ytm']}"
        self.measure('save', now, playlist_id=pid, event_id=receipt, generated=True)
        if isinstance(recipe, dict) and recipe.get('opportunity_id') and recipe.get('option_id'):
            for kind in ('choice', 'save'):
                self.respond(recipe['opportunity_id'], kind, now, option_id=recipe['option_id'],
                             action_id=f'{receipt}:{kind}', details={'playlist_id': pid, 'receipt_id': receipt,
                                                                   'choice_action_id': f'{receipt}:choice'})
            self.refresh()
        if isinstance(recipe, dict):
            if recipe.get('model') == 'fresh' or recipe.get('lane') == 'fresh':
                self.enrich(pid)
            if recipe.get('mode_id') is not None:
                try:
                    self.modes.log_pick(pid, int(recipe['mode_id']), self.now(), ranker=recipe.get('ranker'))
                except Exception:
                    pass  # A mode-pick logging failure must preserve the creation receipt.

    def road_trip(self, drafts, recipe_id, state, name, tracks, recipe, intent):
        result = self.create(name, tracks, recipe=recipe, navigate=True, intent=intent)
        if result['status'] == 'confirmed':
            drafts.saved(recipe_id, state, result['new_ytm'])
        return result

    def home(self, name, tracks, recipe, *, intent=None):
        if not tracks:
            raise ValueError('Keep at least one track to create a playlist.')
        recordings = [track['recording_id'] for track in tracks if isinstance(track, dict)
                      and type(track.get('recording_id')) is int and 0 < track['recording_id'] < 2**63]
        if self.mix_playback is not None and recordings:
            playable = self.mix_playback(recordings)
            selected = []
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                recording = track.get('recording_id')
                if recording is None:
                    selected.append(track)
                elif type(recording) is int and (detail := playable.get(recording)):
                    selected.append({**track, **detail})
            tracks = selected
            if not tracks:
                raise ActionInputError('mix_tracks_unavailable')
        account_id, client = self.access.resolve()
        destination = None

        def remember(playlist, _title):
            nonlocal destination
            destination = playlist

        try:
            return self.create(name, tracks, account_id=account_id, client=client, recipe=recipe,
                               intent=intent, navigate=True, on_created=remember)
        except PlaylistAddUncertain as failure:
            failure.selected_tracks = tracks
            raise
        except Exception as exc:
            if destination is None:
                raise
            attempted = [track for track in tracks if isinstance(track, dict) and track.get('video_id')]
            raise MixSaveIncomplete(account_id, destination, attempted) from exc

    def reconcile(self, recovery, tracks):
        _, client = self.access.resolve(account_id=int(recovery['account_id']))
        detail = validate_playlist(client.get_playlist(recovery['playlist'], limit=None))
        return reconciled_additions(recovery, tracks, {track['videoId'] for track in detail['tracks']})

    def saved(self, account_id, remote_id):
        return self.query_destination(account_id, remote_id)
