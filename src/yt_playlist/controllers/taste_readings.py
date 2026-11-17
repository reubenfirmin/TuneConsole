"""Capture named Taste inputs before computing neutral display models."""
import time

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import taste_display as display


class TasteReadings:
    def __init__(self, *, queries, analysis, parameter, configuration, files=False):
        self.q, self.a, self.parameter, self.configuration = queries, analysis, parameter, configuration
        self.files = files

    def mix_choices(self, now=None):
        if preview := self.a.group_prepared():
            return self.a.group_choices(time.time() if now is None else now, preview)
        evidence = self.a.choice_evidence(now)
        profiles = self.a.mode_profiles()
        rows = {r['mode_id']: {**r, 'profile': profiles[r['mode_id']]} for r in evidence['rows']}
        contrast = evidence['contrast']
        return {**evidence, 'rows': list(rows.values()),
            'strongest': [rows[r['mode_id']] for r in evidence['strongest']],
            'contrast': ({**contrast,
                'favored': [rows[r['mode_id']] for r in contrast['favored']],
                'less_supported': [rows[r['mode_id']] for r in contrast['less_supported']]}
                if contrast is not None else None)}

    def _stored_examples(self, now):
        head = self.q.recommendations.head('home')
        examples = dict.fromkeys(('wheelhouse', 'explore', 'fresh', 'comfort'))
        if head is None:
            return examples
        muted = self.q.model.muted_artists()
        for source in examples:
            after = -1
            while True:
                check_cancellable_work()
                page = self.q.recommendations.candidate_page('home', 'all', source,
                    generation=head['id'], after=after, limit=128)
                blocked = self.a.home_exclusions([t['key'] for t in page['tracks']], now)
                example = next((t for t in page['tracks'] if t['key'] not in blocked and t.get('artist') not in muted), None)
                if example is not None:
                    examples[source] = example
                after = page['after']
                if example is not None or page['exhausted']:
                    break
        return examples

    def generated_playlists(self, now):
        if self.files:
            examples = self._stored_examples(now)
        else:
            bundles = self.q.proposals.get_proposals('mode_bundles') or {}
            blocked, muted = self.q.model.suppressed_keys('for_you', now), self.q.model.muted_artists()
            examples = {source: next((t for t in bundles.get('all', {}).get(source, [])
                if t.get('key') not in blocked and t.get('artist') not in muted), None)
                for source in ('wheelhouse', 'explore', 'fresh', 'comfort')}
        return {'choices': self.mix_choices(now), 'examples': examples,
            'taste_rank_ready': bool(self.a.playlist_taste()),
            'catalog_content_percent': self.configuration.catalog_content_percent,
            **{key: self.parameter(name) for key, name in (
                ('comfort_min_plays', 'comfort_min_plays'), ('comfort_recency_days', 'comfort_recency_full_days'),
                ('artist_cap', 'modes_artist_cap'), ('album_cap', 'modes_album_cap'), ('fresh_artist_cap', 'fresh_artist_cap'))}}

    def source_weights(self, now):
        half = self.parameter('weight_revert_halflife_d')
        return display.source_weights(self.q.model.get_weights(now=now, revert_halflife_d=half),
            self.q.model.weight_records(), now, halflife=half,
            lane_definitions=self.configuration.lanes, lane_default=self.configuration.lane_default)

    def overview(self, now, profiles=None):
        breadth = self.a.breadth()
        if preview := self.a.group_prepared():
            grouped = self.a.group_layers(now, preview)
            shares = {s['mode_id']: s['share'] for s in grouped['session']['segments']}
            n, modes = grouped['session']['n'], grouped['modes']
            current = modes
        else:
            shares, n, current = self.a.session_mix(now)
            modes = self.q.modes.list_modes(active_only=True)
        week = now - 7 * 86400
        return display.overview(breadth, modes=modes, shares=shares, n=n, current_modes=current, profiles=profiles,
            coverage=self.q.history.listening_comparison_coverage(now - 86400, now, exclude_list_ids=self.a.radio_ids()),
            ratings=self.q.model.recent_rating_inputs(week, now),
            changes=self.q.signals.recent_graduations(limit=3, since=week, until=now))

    def _artist_shares(self, top=12):
        return display._artist_shares(self.q.charts.top_artists(limit=1_000_000), top)

    def mode_layers(self, now):
        if preview := self.a.group_prepared():
            return self.a.group_layers(now, preview)
        window = self.parameter('now_window_h')
        recent = self.a.recent(now, window_h=max(24, window))
        now_shares, now_n, current = self.a.now_mix(now, evidence=recent)
        session_shares, session_n, _ = self.a.session_mix(now, evidence=recent)
        return display.mode_layers(self.q.modes.list_modes(active_only=True),
            now_shares=now_shares, now_n=now_n, current_modes=current,
            session_shares=session_shares, session_n=session_n, evidence=self.a.coverage(now, evidence=recent),
            now_window=window, session_half=self.parameter('session_halflife_h'), minimum=self.parameter('now_min_events'))

    def _sources(self, now):
        limit = self.parameter('recent_play_limit')
        half = {source: self.parameter(source + '_halflife_d') for source in ('mood', 'play', 'like', 'dislike', 'skip')}
        weights = {source: self.parameter(source + '_transient_w') for source in ('play', 'like', 'dislike', 'skip')}
        weights['mood'] = 1.
        return display._sources(now, mood_events=self.q.signals.recent_mood_events(), plays=self.a.evidence_plays(limit),
            likes=self.q.model.recent_liked_with_ts(limit=limit), dislikes=self.q.model.disliked_with_ts(),
            skips=self.q.player_events.recent_skips_with_ts(now - 4 * half['skip'] * 86400), half=half, weights=weights)

    def learning_progress(self, now):
        return display.learning_progress(now, threshold=self.parameter('theme_threshold'), entries=self.q.signals.theme_rows(),
            sources=self._sources(now), choices=self.q.modes.choice_learning_rows(), weight_sources=self.source_weights(now),
            changes=self.q.signals.recent_graduations(limit=50), lane_max=self.configuration.lane_max)

    def model_transparency(self, now):
        sources = self.source_weights(now)
        return display.model_transparency(sources=sources,
            weights=self.q.model.get_weights(now=now, revert_halflife_d=sources['lane_weight_halflife_days']),
            standing=self.q.model.get_leans(), leans=self.a.facet_leans(now),
            theme={r['facet']: r['score'] for r in self.q.signals.theme_rows()},
            fparams=tuple(self.parameter(n) for n in ('facet_gain', 'facet_mult_min', 'facet_mult_max')),
            graduation_threshold=self.parameter('theme_threshold'), bd=self.a.breadth(), era_shares=self.a.era_shares(),
            artist_shares=self._artist_shares(), modes=self.mode_layers(now), activity_sources=self._sources(now))

    def engine_panel(self):
        return display.engine_panel(self.q.model.rec_vectors_count(), self.q.settings.get_setting('rec_dim'),
            self.q.settings.get_setting('rec_embed_method'), default_dimension=self.configuration.training_dim)

    def playlist_influence_panel(self, top=8):
        taste = self.a.playlist_taste()
        genres, axes = {}, {}
        stats = self.q.charts.get_playlist_listen_stats() if taste else {}
        if taste:
            order = sorted((i for i, w in enumerate(taste.weights) if w > 0),
                           key=lambda i: (-float(taste.weights[i]), taste.titles[i].casefold()))
            for i in order[:top]:
                pid = taste.pids[i] if i < len(taste.pids) else None
                genres[pid] = self.q.query.playlist_track_genres(pid) if pid is not None else []
                axes[pid] = self.a.axes(genres[pid])
        return display.playlist_influence_panel(taste, stats, genres, axes, top)

    def centroid_tilt_panel(self, now, top=6):
        half, play_half = self.parameter('session_halflife_h'), self.parameter('play_halflife_d')
        keys, matrix, index = self.a.vectors()
        tilts = {'session': None, 'transient': None}
        genres, axes = {}, {}
        if matrix is not None:
            tilts = {'session': self.a.session_tilt(now, matrix, index),
                     'transient': self.a.transient_tilt(now, matrix, index)}
            if any(t is not None for t in tilts.values()):
                genres = self.q.query.track_genres(list(keys))
                axes = self.a.axes(genres.values())
        return display.centroid_tilt_panel(keys, matrix, index, tilts=tilts, genres=genres, genre_axes=axes,
                                           halflife_h=half, play_halflife_d=play_half, top=top)
