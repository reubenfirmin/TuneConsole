"""Shared generated-playlist command and read-only recovery boundary."""
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


class GeneratedPlaylists:
    def __init__(self, access, jobs, modes, *, create, now, measure, respond, enrich,
                 navigate, refresh, query, recipes=None, begin_intent=lambda: None):
        self.access, self.jobs, self.modes = access, jobs, modes
        self.create_destination, self.now, self.measure = create, now, measure
        self.respond, self.enrich, self.navigate, self.refresh = respond, enrich, navigate, refresh
        self.query_destination = query
        self.recipes = recipes
        self.begin_intent = begin_intent

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
        if pid and result.get('added'):
            self.measure('save', self.now(), playlist_id=pid,
                         event_id=f"generate:{result['new_ytm']}", generated=True)
            if isinstance(recipe, dict) and recipe.get('opportunity_id') and recipe.get('option_id'):
                for kind in ('choice', 'save'):
                    self.respond(recipe['opportunity_id'], kind, self.now(), option_id=recipe['option_id'],
                                 action_id=f"generate:{result['new_ytm']}:{kind}", details={'playlist_id': pid})
                self.refresh()
        if pid and isinstance(recipe, dict):
            if recipe.get('model') == 'fresh' or recipe.get('lane') == 'fresh':
                self.enrich(pid)
            if recipe.get('mode_id') is not None:
                try:
                    self.modes.log_pick(pid, int(recipe['mode_id']), self.now(), ranker=recipe.get('ranker'))
                except Exception:
                    pass  # Optional telemetry must not change a confirmed creation receipt.

    def road_trip(self, drafts, recipe_id, state, name, tracks, recipe, intent):
        result = self.create(name, tracks, recipe=recipe, navigate=True, intent=intent)
        if result['status'] == 'confirmed':
            drafts.saved(recipe_id, state, result['new_ytm'])
        return result

    def home(self, name, tracks, recipe, *, intent=None):
        if not tracks:
            raise ValueError('Keep at least one track to create a playlist.')
        return self.create(name, tracks, recipe=recipe, intent=intent, navigate=True)

    def reconcile(self, recovery, tracks):
        _, client = self.access.resolve(account_id=int(recovery['account_id']))
        detail = validate_playlist(client.get_playlist(recovery['playlist'], limit=None))
        return reconciled_additions(recovery, tracks, {track['videoId'] for track in detail['tracks']})

    def saved(self, account_id, remote_id):
        return self.query_destination(account_id, remote_id)
