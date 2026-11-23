"""Complete Road Trip page, recipe, acquisition and generated-save commands."""
from collections import Counter
from copy import deepcopy

from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.rec.road_trip import draft_tracks
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.artist_policy import token
from yt_playlist.core.road_trip_contract import youtube_artist_choice
from yt_playlist.util.navigation import playlist_url
from yt_playlist.util.thumbnails import best_thumb


BLANK_RECIPE = {'id': None, 'name': '', 'own_pct': 50, 'familiarity_pct': 50,
                'target_minutes': 60, 'artists': [], 'genres': [], 'blacklist_genres': []}


class RoadTripCommands:
    def __init__(self, recipes, drafts, *, account, generated, artist_genre, logger, artist_identity=None):
        self.recipes, self.drafts = recipes, drafts
        self.account, self.generated, self.artist_genre, self.logger = account, generated, artist_genre, logger
        self.artist_identity = artist_identity

    def client(self):
        try:
            return self.account()[1]
        except ValueError:
            return None

    def page(self, recipe_id=None, *, error=None, blank=False):
        entry = None if blank else self.drafts.view_with_measurement(recipe_id)
        draft = None
        if entry:
            draft = {**entry, 'state': {**entry['state'], 'build_error': bool(entry['state'].get('build_error'))}}
        loaded = recipe_id if recipe_id is not None else (draft or {}).get('recipe_id')
        recipe = None if blank or loaded is None else self.recipes.get_road_trip_recipe(loaded)
        return {'recipes': self.recipes.list_road_trip_recipes(), 'draft': draft,
                'error': error, 'form_recipe': recipe or dict(BLANK_RECIPE)}

    def save_recipe(self, *args, **kwargs):
        return self.drafts.save_recipe(*args, **kwargs, client=self.client())

    def build(self, recipe_id):
        recipe = self.recipes.get_road_trip_recipe(recipe_id)
        if recipe is None:
            return {'missing': True}
        client = self.client()
        if client is None:
            return {'error': 'Connect an account to build a playlist.'}
        state = self.drafts.build(recipe_id, client)
        if state is None:
            return {'missing': True}
        if not state['building'] and not state['picks']:
            return {'error': f'Couldn\'t build "{recipe["name"]}" - no tracks found.'}
        return {}

    def tilt(self, recipe_id, party, axis, share):
        return self.drafts.tilt(recipe_id, party, axis, share, self.client())

    def unpin(self, recipe_id, party, axis):
        return self.drafts.unpin(recipe_id, party, axis, self.client())

    def reroll(self, recipe_id, index):
        return self.drafts.reroll(recipe_id, index, self.client())

    def shuffle(self, recipe_id):
        return self.drafts.shuffle(recipe_id, self.client())

    def delete(self, recipe_id, *, recipe=False):
        self.drafts.delete(recipe_id, recipe=recipe)

    def admit_save(self):
        return self.generated().admit()

    def save(self, recipe_id, intent, launch_id=''):
        recipe, state = self.drafts.for_save(recipe_id)
        result = {'name': f'Road Trip: {recipe["name"]}' if recipe else 'Road Trip', 'launch_id': launch_id}
        if recipe is None or state is None:
            return {**result, 'error': 'That playlist is gone - build it again.'}
        tracks = draft_tracks(state)
        if not tracks:
            return {**result, 'error': "Couldn't save it - connect the main account and keep at least one track."}
        stats = state['stats']
        evidence = {'model': 'road_trip', 'road_trip_recipe_id': recipe_id, 'journey': 'road_trip',
                    **{key: state[key] for key in ('own_pct', 'familiarity_pct', 'target_minutes')},
                    'achieved_minutes': stats.get('minutes'), 'own_count': stats.get('own_count'),
                    'their_count': stats.get('their_count')}
        if 'version_policy' in state:
            evidence['version_policy'] = deepcopy(state['version_policy'])
        try:
            saved = self.generated().road_trip(self.drafts, recipe_id, state,
                                              result['name'], tracks, evidence, intent)
            result.update(ytm=saved['new_ytm'], pid=saved['pid'], added=saved['added'])
            result['destination_url'] = saved.get('playlist_url') or playlist_url(saved['pid'])
            if saved['status'] == 'rejected':
                result['error'] = 'No songs were added to this playlist.'
        except PlaylistAddUncertain as exc:
            result.update(error=str(exc), partial_ytm=exc.playlist_id)
        except ActionInputError as exc:
            result['error'] = str(exc)
        except Exception:
            self.logger.exception('save road trip %s failed', recipe_id)
            result['error'] = 'YouTube returned an unexpected response.'
        return result

    def autocomplete(self, query):
        if not query or (client := self.client()) is None:
            return []
        try:
            rows = client.search(query, filter='artists') or []
        except Exception:
            return []
        options = {}
        for row in rows[:8]:
            name = row.get('artist') or row.get('title')
            if not name:
                continue
            channel = row.get('browseId')
            selection = youtube_artist_choice(channel, name)
            if selection is None:
                options.setdefault((None, name), (name, None, None))
                continue
            identity = self.artist_identity(channel) if self.artist_identity else None
            key = selection if identity is None else token(identity)
            options.setdefault((channel, None), ({'key': key, 'name': name}, channel, row.get('thumbnails')))
        names = Counter((option['name'] if channel else option).casefold()
                        for option, channel, _ in options.values())
        for option, channel, thumbnails in options.values():
            if channel and names[option['name'].casefold()] > 1:
                option.update(channel=channel, thumbnail=best_thumb(thumbnails))
        return [option for option, _, _ in options.values()]
