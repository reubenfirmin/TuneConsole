"""Capture named Taste inputs before computing neutral display models."""
import time

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.rec_model_policy import PERMANENT_SURFACE
from yt_playlist.rec import taste_display as display


class TasteReadings:
    def __init__(self, *, queries, analysis, parameter, configuration):
        self.q, self.a, self.parameter, self.configuration = queries, analysis, parameter, configuration

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
        return {'choices': self.mix_choices(now), 'examples': self._stored_examples(now),
            'taste_rank_ready': bool(self.a.playlist_taste()),
            'catalog_content_percent': self.configuration.catalog_content_percent,
            **{key: self.parameter(name) for key, name in (
                ('comfort_min_plays', 'comfort_min_plays'), ('comfort_recency_days', 'comfort_recency_full_days'),
                ('artist_cap', 'modes_artist_cap'), ('album_cap', 'modes_album_cap'), ('fresh_artist_cap', 'fresh_artist_cap'))}}

    def source_weights(self, now):
        half = self.parameter('weight_revert_halflife_d')
        return display.source_weights(self.q.model.get_weights(now=now, revert_halflife_d=half),
            self.q.model.weight_records(), now, halflife=half,
            lane_definitions=self.configuration.lanes, lane_default=self.configuration.lane_default,
            session=self.q.model.get_session_weights(now))

    def overview(self, now, profiles=None):
        breadth = self.a.breadth()
        if preview := self.a.group_prepared():
            grouped = self.a.group_layers(now, preview)
            shares = {s['mode_id']: s['share'] for s in grouped['session']['segments']}
            n, modes = grouped['session']['n'], grouped['modes']
            current = modes
        else:
            shares, n, current, modes = {}, 0, [], []
        week = now - 7 * 86400
        return display.overview(breadth, modes=modes, shares=shares, n=n, current_modes=current, profiles=profiles,
            coverage=self.a.today_coverage(now),
            ratings=self.q.model.recent_rating_inputs(week, now),
            changes=self.q.signals.recent_graduations(limit=3, since=week, until=now))

    def _artist_shares(self, top=12):
        return display._artist_shares(self.q.charts.top_artists(limit=1_000_000), top)

    def mode_layers(self, now):
        preview = self.a.group_prepared() or {'generation': None, 'groups': []}
        return self.a.group_layers(now, preview, observed=True)

    def _sources(self, now):
        half = {source: self.parameter(source + '_halflife_d') for source in ('mood', 'play', 'like', 'skip')}
        weights = {source: self.parameter(source + '_transient_w') for source in ('play', 'like', 'skip')}
        weights['mood'] = 1.
        mood, plays, likes, skips = self.a.recent_sources(now)
        return display._sources(now, mood_events=mood, plays=plays, likes=likes,
                                skips=skips, half=half, weights=weights)

    def learning_progress(self, now):
        published = self.q.proposals.get_proposals(PERMANENT_SURFACE) or None
        entries = self.q.signals.theme_rows() if published is None else [
            {'facet': axis, **reading, 'score': reading['support'],
             'updated_at': published['through']} for axis, reading in published['facets'].items()]
        return display.learning_progress(now, threshold=self.parameter('theme_threshold'), entries=entries,
            sources=self._sources(now), choices=self.q.modes.choice_learning_rows(), weight_sources=self.source_weights(now),
            changes=self.q.signals.recent_graduations(limit=50), lane_max=self.configuration.lane_max,
            dated=published is not None)

    def model_transparency(self, now):
        sources = self.source_weights(now)
        published = self.q.proposals.get_proposals(PERMANENT_SURFACE) or None
        return display.model_transparency(sources=sources, **self.a.facet_breakdown(now),
            theme={r['facet']: r['score'] for r in self.q.signals.theme_rows()},
            support=None if published is None else published['facets'],
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
