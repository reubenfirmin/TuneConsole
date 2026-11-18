"""Suggestion queries, exposure and feedback with explicit application capabilities."""
from yt_playlist.rec.suggestion_policy import feedback_axis, feedback_change, mood_window


class Suggestions:
    def __init__(self, playlists, query, discovery, model, *, complete, neighbors, proxy, record, rebuild):
        self.playlists, self.query, self.discovery, self.model = playlists, query, discovery, model
        self.complete, self.neighbors, self.proxy = complete, neighbors, proxy
        self.record, self.rebuild = record, rebuild

    def completion(self, playlist_id, now):
        if self.playlists.get_playlist(playlist_id) is None:
            raise LookupError('playlist not found')
        items = self.complete(playlist_id, now)
        self.record('playlist_completion', items, now, lane='complete', ranker='artist-genre',
                    provenance={'playlist_id': playlist_id})
        return {'suggestions': items, 'pid': playlist_id}

    def similar(self, video, playlist_id, now):
        key = self.query.key_for_video(video)
        neighbors = self.neighbors(key, topn=12) if key else []
        if not neighbors and key:
            neighbors = self.proxy(key, topn=12)
        keys = [k for k, _ in neighbors]
        meta = self.query.tracks_by_keys(keys + ([key] if key else []))
        pool = self.discovery.discovered_tracks_by_keys([k for k in keys if k not in meta])
        scores = dict(neighbors)
        self.record('similar', [{**item, 'key': k, 'score': scores[k]} for k in keys
            if (item := meta.get(k) or pool.get(k))], now, lane='similar',
            ranker='blended-neighbors', provenance={'seed': key})
        return {'items': [meta.get(k) or pool[k] for k in keys if k in meta or k in pool],
                'seed': meta.get(key, {}), 'have_model': self.model.rec_vectors_count() > 0,
                'pid': playlist_id, 'seed_vid': video}

    def request_rebuild(self):
        if self.rebuild is None:
            return {'ok': False, 'queued': False, 'error': 'Recommendation worker unavailable'}
        self.rebuild()
        return {'ok': True, 'queued': True, 'count': self.model.rec_vectors_count()}


class SuggestionFeedback:
    def __init__(self, model, signals, playlists, recipes, *, facts, parameter, lanes, facet, moods):
        self.model, self.signals, self.playlists, self.recipes = model, signals, playlists, recipes
        self.facts, self.parameter, self.lanes = facts, parameter, lanes
        self.facet, self.moods = facet, moods

    def feedback(self, item, kind, reason, surface, scope, lane, explicit_axis, now):
        change = feedback_change(kind, reason, lane, now, self.lanes)
        self.model.record_feedback(surface, item, kind, reason=reason, scope=scope,
                                   until=change['until'], now=now)
        if reason != 'own_it':
            facts = {} if explicit_axis and explicit_axis.split(':', 1)[0] in ('genre', 'era', 'artist', 'pop') else self.facts(reason, item)
            axis = feedback_axis(explicit_axis, reason, **facts)
            if axis:
                self.facet(axis, change['signed'], now, source=self.parameter('source_w_feedback'), source_label='feedback')
        if change['lane'] and change['factor']:
            self.model.nudge_weight(f"lane:{change['lane']}", change['factor'], now=now,
                                    revert_halflife_d=self.parameter('weight_revert_halflife_d'))

    def mood(self, keys, signed, now, *, playlist_id=None):
        if playlist_id is not None:
            keys = self.playlists.get_playlist_track_keys(playlist_id)
        if not keys:
            return
        keys = list(keys)
        self.signals.record_mood(keys, signed, now,
            prune_before=mood_window(now, self.parameter('mood_halflife_d')))
        self.moods(keys, signed, now, source=self.parameter('source_w_vibe'))

    def journey(self, playlist_id, direction, now):
        playlist = self.playlists.get_playlist(playlist_id)
        recipe = self.recipes.get_recipe(playlist.playlist_scope_id) if playlist else None
        if journey := (recipe or {}).get('journey'):
            self.facet(f'journey:{journey}', 1 if direction >= 0 else -1, now)
