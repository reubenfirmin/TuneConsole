"""Coordinate prediction domain plans, sample/model files and SQL owners."""
import json

from yt_playlist.core import audio_vibe_metadata, sample_files, vibe_manifest, vibe_model_files, vibe_publication
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS, PREDICTION_VERSION


class VibePredictions:
    def __init__(self, repository, provenance, samples, metadata, models, settings, refresh, prepared_content, genres):
        self.repository, self.provenance, self.samples, self.metadata = repository, provenance, samples, metadata
        self.models, self.settings, self.refresh = models, settings, refresh
        self.prepared_content, self.genres = prepared_content, genres

    def prepare_publication(self, tracks, resolved, touched, now):
        versions = {track['version'] for track in tracks.values() if track}
        requests = [(key, track['version'], track['evidence']['input_digest'])
                    for key, track in tracks.items() if track and track['evidence']]
        originals = {original for fits in resolved.values() for fit in fits.values()
                     if not fit.get('retain') and not fit.get('generation')
                     and fit['source'] in ('artist_genre', 'artist', 'genre')
                     for original in fit['inputs'].values() if original is not None}
        artists = {artist for track in tracks.values() if track for artist in json.loads(track['artist'] or '[]')}
        facts = self.repository.publication_facts(list(tracks), versions, requests, originals, artists)
        axes = self.genres.feature_axes({track['genre'] for track in tracks.values() if track and track['genre']})
        plan = vibe_publication.prepare(tracks, resolved, touched, now, facts, axes)
        plan.staged, plan.markers, plan.roots = self.provenance.stage_many(plan.provenance)
        return plan

    def publish_prepared(self, plan):
        self.repository.assert_idle()
        for publication in plan.staged:
            publication.publish()
        self.repository.publish_prepared(plan,
            cohorts=sorted(key for key in plan.cohorts if not key.startswith('!joint:')))
        repairs = {key.removeprefix('!joint:') for key in plan.cohorts if key.startswith('!joint:')}
        if repairs:
            self.settings.publish_values({vibe_model_files.STATE_PREFIX + vibe: '' for vibe in repairs})
        if plan.deleted or plan.publications:
            self.refresh.publish_notification()

    def pending(self):
        return self.repository.pending()

    def fanout(self, limit=32):
        return self.repository.fanout(limit)

    def has_joint_scores(self, generations):
        return self.repository.has_joint_scores(generations)

    def ensure_version(self, version):
        if self.settings.get_setting('rec_vibe_version') == version:
            return False
        self.repository.queue(['*'])
        self.settings.publish_values({'rec_vibe_version': version})
        return True

    def _current(self, rid):
        return vibe_publication.current(*self.repository.current_facts(rid))

    def inputs(self, keys, max_bytes=8 * 1024 * 1024):
        result, used = {}, 0
        with self.metadata.retained():
            for key, facts in self.repository.input_facts(keys).items():
                samples, metadata = [], {}
                for rows, source in facts['runs']:
                    captured = []
                    for row in rows:
                        try:
                            sample = self.samples.read(row)
                        except (OSError, ValueError):
                            break
                        used += len(sample_files.canonical(sample))
                        if used > max_bytes:
                            raise WorkBudgetExceeded('Vibe measurements exceed input budget')
                        captured.append(sample)
                    if len(captured) == 3:
                        samples = captured
                        metadata = audio_vibe_metadata.assemble(source,
                            (self.metadata.source_values(reference) for reference in source['sources']))
                        break
                artists = facts['artists']
                result[key] = {'old': vibe_publication.current(*facts['old']),
                    'artist': json.dumps(artists, separators=(',', ':')) if 0 < len(artists) <= 16 else None,
                    'samples': samples, 'metadata': metadata, 'recording': facts['recording']}
        return result

    def groups(self, keys):
        result = {}
        for key in keys:
            stats, inputs, after = {}, {}, (0, '')
            while True:
                check_stopping()
                rows = self.repository.cohort_rows(key, after, 128)
                vibe_publication.add_cohort(stats, inputs, [row[1:] for row in rows])
                if len(rows) < 128:
                    break
                after = rows[-1][0], rows[-1][3]
            result[key] = {'stats': stats, 'inputs': inputs}
        return result

    def joint_inputs(self, vibe, limit):
        rows = self.repository.joint_training_rows(vibe, limit)
        if len(rows) > limit:
            raise WorkBudgetExceeded('Joint vibe original manifest exceeds bounded admission')
        albums = self.repository.album_facts([row['recording_id'] for row in rows])
        return vibe_manifest.training(rows, albums, limit=limit)

    def joint_profile_rows(self, recordings):
        keys = self.repository.joint_profile_keys(recordings)
        return keys, self.prepared_content.capture([(key, 0) for key in keys.values()], mask_numeric=True)

    def joint_albums(self, keys):
        identities, facts = self.repository.joint_album_facts(keys)
        albums = vibe_manifest.albums(facts)
        return {key: albums.get(recording) for key, recording in identities.items()}

    def stage_joint_model(self, value):
        return self.models.stage(value)

    def joint_model(self, generation):
        return self.models.read(generation)

    def publish_joint_model(self, vibe, generation, outcome, measurements, staged, refresh):
        self.repository.assert_idle()
        if staged:
            staged.publish()
        self.repository.root_joint_model(measurements, refresh)
        self.settings.publish_values({vibe_model_files.HEAD_PREFIX + vibe: generation,
                                     vibe_model_files.STATE_PREFIX + vibe: json.dumps(outcome)})

    def defer_joint_model(self, vibe, outcome, refresh):
        if refresh:
            self.repository.queue(['*'])
        self.settings.publish_values({vibe_model_files.STATE_PREFIX + vibe: json.dumps(outcome)})

    def retained_joint_fits(self, keys):
        return {key: {row['vibe']: {**{name: row[name] for name in ('score', 'support', 'source', 'recordings', 'variance')},
                                   'retain': True} for row in rows}
                for key, rows in self.repository.retained_joint_fits(keys).items()}

    def overlay(self, profiles):
        if not profiles:
            return profiles
        for key, row in self.repository.overlay_rows(list(profiles)):
            vibes = profiles[key].setdefault('vibes', {'version': PREDICTION_VERSION,
                'inputs_version': row['inputs_version'], 'fits': {}})
            fit = {name: row[name] for name in ('score', 'support', 'source')}
            if row['recordings'] is not None:
                fit.update({name: row[name] for name in ('recordings', 'variance')})
            if row['vibe'] in INTENSITY_CHANNELS:
                vibes.setdefault('intensity_fits', {})[INTENSITY_CHANNELS[row['vibe']]] = fit
            else:
                vibes['fits'][row['vibe']] = fit
        return profiles

    def fit_scores(self, keys, preset):
        return self.repository.fit_scores(keys, preset)

    def ranked(self, vibe, limit=100):
        return self.repository.ranked(vibe, limit)
