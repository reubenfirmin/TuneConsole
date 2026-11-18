"""Lazy Taste queries. Feature readings own their capture; composition supplies repositories."""
from yt_playlist.controllers.taste import COMPARISON_SETTING, baseline_rows, comparison_rows
from yt_playlist.rec import rec_params


class TasteQueries:
    PANELS = frozenset({'overview', 'groups', 'profile', 'generated', 'recommendations', 'learning', 'health', 'feedback'})

    def __init__(self, model, tracks, discovery, proposals, modes, evidence, history, settings,
                 *, readings, parameter, breadth, ranked, tuning):
        self.model, self.tracks, self.discovery, self.proposals = model, tracks, discovery, proposals
        self.modes, self.evidence, self.history, self.settings = modes, evidence, history, settings
        self.readings, self.parameter, self.breadth = readings, parameter, breadth
        self.ranked, self.tuning = ranked, tuning

    def controls(self, now, groups):
        return {'values': {spec.name: self.parameter(spec.name) for spec in rec_params.PARAMS
                           if spec.group in groups or spec.name in rec_params.SEMANTIC_PARAMS},
                'adaptive': bool(self.readings.group_checkpoint()), 'tuning': self.tuning(now)}

    def model_status(self):
        breadth = self.breadth()
        total = self.tracks.tracks_total()
        tagged = sum(self.tracks.genre_distribution().values())
        return {**self.readings.engine(), 'content_maintenance': self.readings.maintenance(),
                'tracks_total': total, 'tagged': tagged, 'coverage': tagged / total if total else 0.,
                'breadth': breadth['breadth'], 'breadth_tagged': breadth['n_tagged'],
                'breadth_families': breadth['n_families'], 'baskets': len(self.evidence.rec_baskets())}

    def feedback(self):
        bans = self.model.list_dislikes()
        keys = [row['item_key'] for row in bans]
        metadata = self.tracks.tracks_by_keys(keys)
        metadata.update(self.discovery.discovered_tracks_by_keys([key for key in keys if key not in metadata]))
        tracks = []
        for row in bans:
            title, _, artist = row['item_key'].partition('|')
            tracks.append({'title': title or 'Unknown track', 'artist': artist or 'Unknown artist',
                'album': '', 'thumbnail': None, 'video_id': None, **metadata.get(row['item_key'], {}),
                'key': row['item_key'], 'until': row['until']})
        return {'feedback': {kind: count for kind, count in self.model.feedback_summary().items()
                             if not kind.endswith('_grad')}, 'bans': tracks,
                'album_feedback': self.readings.album_feedback()}

    def recommendations(self, now):
        weights = self.model.get_weights(now=now, revert_halflife_d=self.parameter('weight_revert_halflife_d'))
        return {'genres': genre_rows(self.breadth()['families'], weights, self.model.weight_records(), now),
                'genre_min': rec_params.GENRE_MIN, 'genre_max': rec_params.GENRE_MAX,
                'genre_default': rec_params.GENRE_DEFAULT, 'genre_step': rec_params.GENRE_STEP,
                'controls': self.controls(now, ('discovery',))}

    def grouping(self, *, policy=False):
        result = {'grouping': self.readings.group_view(), 'group_progress': self.readings.group_progress()}
        if policy:
            result['group_policy'] = self.readings.group_policy()
        return result

    def maintenance(self):
        return {'content_maintenance': self.readings.maintenance()}

    def recent_sound(self, now):
        return {'tilt': self.readings.recent_sound(now)}

    def playlist_influence(self):
        return {'influence': self.readings.playlist_influence()}

    def preview(self, now):
        return {'items': self.readings.sample(now, limit=8),
                'lane_labels': {name: label for name, label, _ in rec_params.LANES}}

    def recall(self, now, *, failed=False):
        return {**self.readings.health(now, failed=failed),
                'grad_counts': self.readings.graduation_counts(),
                'served': self.readings.scorecard(now - 30 * 86400, now)}

    def selection(self, now, *, limit=6):
        return {'selection': self.readings.selection(now, limit=limit),
                'autotune_result': self.readings.autotune()}

    def panel(self, panel, now):
        if panel == 'overview':
            total = self.tracks.tracks_total()
            choices = self.readings.mix_choices(now)
            profiles = {row['mode_id']: row['profile'] for row in choices['rows']}
            return {'viz': self.readings.overview(now, profiles=profiles), 'generation': {'choices': choices},
                    'grouping': self.readings.group_view(), 'vectors': self.model.rec_vectors_count(),
                    'coverage': sum(self.tracks.genre_distribution().values()) / total if total else 0.}
        if panel == 'groups':
            return self.grouping(policy=True)
        if panel == 'profile':
            modes = []
            if not self.readings.group_checkpoint():
                rows = self.modes.list_modes(active_only=True)
                metadata = self.tracks.tracks_by_keys([key for mode in rows for key in mode['rep_keys']])
                modes = [{'label': row['label'], 'size': row['size'], 'fresh': now - row['first_seen'] < 2 * 86400,
                          'tracks': [metadata[key] for key in row['rep_keys'] if key in metadata]} for row in rows]
            return {'viz': self.readings.transparency(now), 'modes': modes}
        if panel == 'generated':
            return {'viz': self.readings.source_weights(now),
                    'listening_choices': self.proposals.get_proposals('listening_choices'),
                    'choice_daily': self.proposals.get_proposals('choice_daily'),
                    'generation': self.readings.generated_playlists(now), 'lane_min': rec_params.LANE_MIN,
                    'lane_max': rec_params.LANE_MAX, 'lane_default': rec_params.LANE_DEFAULT, 'lane_step': .05}
        if panel == 'recommendations':
            return self.recommendations(now)
        if panel == 'learning':
            return {'learning': self.readings.learning(now), 'controls': self.controls(now, ('transient', 'graduation'))}
        if panel == 'health':
            return {**self.model_status(), **self.selection(now), 'spotify_history': self.history.spotify_history_stats()}
        if panel == 'feedback':
            return self.feedback()
        raise KeyError(panel)

    def comparison(self, now):
        baseline = baseline_rows(self.settings.get_setting(COMPARISON_SETTING))
        if baseline is None:
            return {'comparison': None}
        current = self.ranked(now, [row['key'] for row in baseline])
        weights = self.model.get_weights(now=now, revert_halflife_d=self.parameter('weight_revert_halflife_d'))
        return {'comparison': comparison_rows(baseline, current, weights), 'comparison_limit': 24,
                'lane_labels': {name: label for name, label, _ in rec_params.LANES}}


def genre_rows(families, weights, records, now):
    rows = []
    for family, share in sorted(families.items(), key=lambda item: -item[1]):
        record = records.get(f'genre:{family}')
        updated = record['updated_at'] if record else None
        rows.append({'family': family, 'share': share, 'weight': weights.get(f'genre:{family}', 1.),
                     'has_record': record is not None, 'saved_weight': record['weight'] if record else 1.,
                     'age_days': max(0., (now - updated) / 86400) if updated is not None else None})
    return rows
