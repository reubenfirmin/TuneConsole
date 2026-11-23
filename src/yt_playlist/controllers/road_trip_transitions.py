"""Acquire coherent draft inputs before detached Road Trip state transitions."""
from yt_playlist.rec import road_trip_rules as policy
from yt_playlist.rec.road_trip_artists import recipe_inputs
from yt_playlist.rec.road_trip_candidates import own_rows
from yt_playlist.rec.road_trip_ranking import TrackRanking


class RoadTripFacts:
    def __init__(self, query, model, genres, *, rank=None, excluded_videos=None):
        self.query, self.model, self.genres = query, model, genres
        self.rank = rank
        self.excluded_videos = excluded_videos

    def excluded(self, state, now):
        if self.excluded_videos is None:
            return frozenset()
        return self.excluded_videos([*(state.get('pool') or []), *(state.get('picked') or [])], now)

    def own(self, state, now):
        songs = self.query.library_songs()
        plays = self.query.play_counts()
        blocked = self.model.suppressed_keys('for_you', now)
        if blacklist := state.get('blacklist_genres'):
            blocked |= self.query.keys_in_genre_selection(blacklist)
        return own_rows(songs, plays, blocked, self.model.muted_artists(),
                        self.query.artist_genre_years(), state.get('own_facts') or {})

    def axes(self, state, own=()):
        rows = [*(state.get('pool') or []), *(state.get('picked') or []), *own,
                *(state.get('artist_contexts') or {}).values()]
        return self.genres.feature_axes([*state.get('inputs', {}).get('genres', []),
                                        *(row.get('genre') for row in rows)])

    def families(self):
        result = {}
        for row in self.genres.taxonomy()['genres']:
            result.setdefault(row['family'], []).append(row['name'])
        return result

    def ranking(self, state, own, now):
        if self.rank is None:
            return TrackRanking()
        seeds = state.get("inputs") or {}
        passengers = [c for c in state.get("pool") or [] if c["source"] == "theirs"]
        return self.rank(own, passengers, seeds.get("artists") or [], seeds.get("genres") or [], now,
                         artist_refs=seeds.get("artist_refs") or {})


class RoadTripTransitions:
    def __init__(self, inputs):
        self.inputs = inputs

    def capture(self, state, now, *, own_pool=None, families=False):
        with self.inputs() as facts:
            own = facts.own(state, now) if own_pool is None else own_pool
            return policy.DraftInputs(own, facts.axes(state, own), facts.families() if families else {},
                                      ranking=facts.ranking(state, own, now), excluded=facts.excluded(state, now))

    def repick(self, state, now, *, own_pool=None):
        return policy.repick_values(state, self.capture(state, now, own_pool=own_pool))

    def reroll(self, state, index, now):
        return policy.reroll_values(state, index, self.capture(state, now))

    def start(self, recipe, now, seed, previous=None):
        state = {'blacklist_genres': recipe.get('blacklist_genres') or [],
                 'inputs': recipe_inputs(recipe)}
        return policy.start_values(recipe, seed, self.capture(state, now), previous)

    def apply_recipe(self, state, recipe, now):
        # Capture ranking after the transition removes tracks acquired for retired
        # inputs. This also keeps a same-name replacement out of the new seed set.
        return policy.apply_recipe_values(state, recipe, lambda updated: self.capture(updated, now))

    def finish(self, state, now):
        return policy.finish_values(state, self.capture(state, now))

    def normalize(self, state, now):
        with self.inputs() as facts:
            axes = facts.axes(state)
            own = None
            ranking = TrackRanking()
            if policy.normalization_requires_pool(state, axes):
                own = facts.own(state, now)
                axes = facts.axes(state, own)
                ranking = facts.ranking(state, own, now)
            excluded = facts.excluded(state, now)
        return policy.normalized_values(state, policy.DraftInputs(own, axes, {}, ranking=ranking, excluded=excluded))

    def tilt(self, state, party, axis, share, now):
        inputs = self.capture(state, now, families=party == 'theirs')
        return policy.set_share_values(state, party, axis, share, inputs)

    def unpin(self, state, party, axis, now):
        return policy.clear_share_values(state, party, axis, self.capture(state, now))
