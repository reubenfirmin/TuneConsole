"""Coordinate prediction domain plans, sample/model files and SQL owners."""
import json
import logging

from yt_playlist.core import audio_vibe_metadata, fao, sample_files, vibe_manifest, vibe_publication
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.audio_vibe_contract import ACOUSTICBRAINZ_ESTIMATE, INTENSITY_CHANNELS, PREDICTION_VERSION
from yt_playlist.controllers.vibe_prediction import configured
from yt_playlist.rec.vibe_score_policy import score_patterns, serving_pattern

log = logging.getLogger(__name__)


def overlay_profiles(profiles, rows):
    """Attach current relational fits to detached profiles; retain dimensions."""
    for profile in profiles.values():
        profile.pop('vibes', None)
    for key, row in rows:
        vibes = profiles[key].setdefault('vibes', {'version': PREDICTION_VERSION,
            'inputs_version': row['inputs_version'], 'fits': {}})
        fit = {name: row[name] for name in ('score', 'support', 'source')}
        if (row['vibe'] in INTENSITY_CHANNELS or row['prediction_id'] is not None
                or row['source'] == ACOUSTICBRAINZ_ESTIMATE):
            fit['inputs_version'] = row['inputs_version']
        if row['recordings'] is not None:
            fit.update({name: row[name] for name in ('recordings', 'variance')})
        if row['vibe'] in INTENSITY_CHANNELS:
            vibes.setdefault('intensity_fits', {})[INTENSITY_CHANNELS[row['vibe']]] = fit
        else:
            vibes.setdefault('fits', {})[row['vibe']] = fit
    return profiles


class VibePredictions:
    def __init__(self, repository, provenance, samples, metadata, models, refresh, prepared_content, genres,
                 *, file_repository=None):
        self.repository, self.provenance, self.samples, self.metadata = repository, provenance, samples, metadata
        self.models, self.refresh = models, refresh
        self.prepared_content, self.genres = prepared_content, genres
        self.file_repository = file_repository if file_repository is not None else repository

    def prepare_publication(self, tracks, resolved, touched, now):
        requests = [(key, track['version'], track['evidence']['input_digest'])
                    for key, track in tracks.items() if track and track['evidence']]
        originals = {original for fits in resolved.values() for fit in fits.values()
                     if not fit.get('retain') and not fit.get('generation')
                     and fit['source'] in ('artist_genre', 'artist', 'genre')
                     for original in fit['inputs'].values() if original is not None}
        artists = {artist for track in tracks.values() if track for artist in json.loads(track['artist'] or '[]')}
        facts = self.repository.publication_facts(list(tracks), requests, originals, artists)
        axes = self.genres.feature_axes({track['genre'] for track in tracks.values() if track and track['genre']})
        plan = vibe_publication.prepare(tracks, resolved, touched, now, facts, axes)
        records, plan.roots = self.provenance.describe(plan.provenance)
        if records:
            identities = self.file_repository.reserve_provenance(records)
            if len(identities) != len(records):
                raise ValueError('Incomplete prediction provenance reservation')
            plan.file_records = [{**record, 'file_id': identity} for record, identity in zip(records, identities)]
            plan.staged, plan.markers = self.provenance.stage_reserved(plan.file_records)
        return plan

    def maintain(self, now):
        expired = self.repository.expire_provenance(now)
        retired = self.maintain_history(now)
        self.collect_provenance(now)
        self.models.collect(now)
        return expired + retired

    def maintain_history(self, now, limit=128):
        with self.provenance.history() as after:
            if after is None:
                return 0
            retired, following = self.repository.retire_history(now, after, limit)
            self.provenance.save_history(following)
            return retired

    def collect_provenance(self, now, limit=16):
        self.repository.assert_idle()
        examined = 0
        with self.provenance.garbage(limit=limit) as candidates:
            for path, content, identity, group in candidates:
                ready, owned = self.repository.provenance_owners(identity, group)
                self.provenance.prune(path, content, file_id=identity, ready=ready, owned=owned,
                                      before=now - 30 * 86400)
                examined += 1
        return examined

    def publish_prepared(self, plan):
        self.repository.assert_idle()
        for publication in plan.staged:
            publication.publish()
        self.repository.publish_prepared(plan,
            cohorts=sorted(key for key in plan.cohorts if not key.startswith('!joint:')))
        repairs = {key.removeprefix('!joint:') for key in plan.cohorts if key.startswith('!joint:')}
        if repairs:
            self.repository.clear_joint_outcomes(repairs)
        if plan.deleted or plan.publications or plan.estimates or plan.deleted_estimates:
            self.refresh.publish_notification()

    def pending(self):
        return self.repository.pending()

    def fanout(self, limit=32):
        return self.repository.fanout(limit, measurement_pattern=serving_pattern())

    def has_joint_scores(self, generations):
        return self.repository.has_joint_scores(generations)

    def ensure_version(self, version):
        return self.repository.ensure_version(version)

    def rebuild_policy(self):
        return self.repository.rebuild_policy()

    def acousticbrainz_matches(self, keys):
        return self.repository.acousticbrainz_matches(keys)

    def acousticbrainz_training_rows(self, version):
        return self.repository.acousticbrainz_training_rows(version)

    def acousticbrainz_model(self):
        return self.repository.acousticbrainz_model()

    def install_acousticbrainz_model(self, previous, state, *, refresh):
        return self.repository.install_acousticbrainz_model(previous, state, refresh=refresh)

    def acousticbrainz_pending(self):
        return self.repository.acousticbrainz_pending()

    def acousticbrainz_estimate_page(self, cursor, limit):
        return self.repository.acousticbrainz_estimate_page(cursor, limit)

    def publish_acousticbrainz_page(self, updates, cursor):
        return self.repository.publish_acousticbrainz_page(updates, cursor)

    def _current(self, rid):
        return vibe_publication.current(*self.repository.current_facts(rid))

    def inputs(self, keys, max_bytes=8 * 1024 * 1024, *, usable_samples=None):
        """Select verified, compatible excerpts for the current specialists."""
        result, used = {}, 0
        with self.metadata.retained(), self.samples.retained():
            for key, facts in self.repository.input_facts(keys).items():
                samples, selected, source = [], None, None
                for order, rows, metadata_source in self._capture_runs(key, facts):
                    if len(rows) < len(samples):
                        break
                    captured = []
                    for row in rows:
                        try:
                            sample = self.samples.read(row)
                        except (OSError, ValueError):
                            continue
                        used += len(sample_files.canonical(sample))
                        if used > max_bytes:
                            raise WorkBudgetExceeded('Vibe measurements exceed input budget')
                        captured.append(sample)
                    if captured:
                        if usable_samples is None:
                            usable_samples = configured().capture().usable_samples
                        captured = usable_samples(captured)
                    if captured and (selected is None or (len(captured), order) > (len(samples), selected)):
                        samples, selected = captured, order
                        source = metadata_source
                    if len(samples) == 3:
                        break
                metadata = {}
                if source is not None:
                    metadata = audio_vibe_metadata.assemble(source,
                        (self.metadata.source_values(reference) for reference in source['sources']))
                artists = facts['artists']
                result[key] = {'artist': json.dumps(artists, separators=(',', ':')) if 0 < len(artists) <= 16 else None,
                    'samples': samples, 'metadata': metadata, 'recording': facts['recording'],
                    'needs_fresh_audio': bool(facts['runs']) and not samples}
        return result

    def _capture_runs(self, key, facts):
        while True:
            for order, rows in facts['runs'].items():
                yield order, rows, facts['sources'][order[1]]
            if facts['next_run_offset'] is None:
                return
            check_stopping()
            facts = self.repository.input_facts([key], run_offset=facts['next_run_offset'])[key]

    def groups(self, keys):
        result = {}
        pattern = serving_pattern()
        for key in keys:
            stats, inputs, after = {}, {}, (0, '')
            while True:
                check_stopping()
                rows = self.repository.cohort_rows(key, after, 128, pattern=pattern)
                vibe_publication.add_cohort(stats, inputs, [row[1:] for row in rows])
                if len(rows) < 128:
                    break
                after = rows[-1][0], rows[-1][3]
            result[key] = {'stats': stats, 'inputs': inputs}
        return result

    def joint_inputs(self, vibe, limit):
        rows = self.repository.joint_training_rows(vibe, limit, pattern=serving_pattern())
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

    def joint_state(self, vibe):
        return self.repository.joint_state(vibe)

    def joint_revision(self):
        return self.repository.joint_revision()

    def joint_fit_states(self):
        return self.repository.joint_fit_states()

    def publish_joint_model(self, vibe, generation, outcome, measurements, staged, refresh, *, artists=()):
        self.repository.assert_idle()
        if staged:
            self.models.publish(staged)
        self.repository.install_joint_model(vibe, generation, outcome, measurements, refresh, artists=artists)

    def defer_joint_model(self, vibe, outcome, refresh):
        self.repository.save_joint_outcome(vibe, outcome, refresh=refresh)

    def retained_joint_fits(self, keys):
        return {key: {row['vibe']: row for row in rows}
                for key, rows in self.repository.retained_joint_fits(keys, patterns=score_patterns()).items() if rows}

    def retained_conditioning(self, prediction_id):
        try:
            return self.repository.prediction_conditioning(prediction_id)
        except (OSError, fao.CorruptFile):
            log.warning('Unable to verify retained Vibe prediction conditioning id=%s', prediction_id, exc_info=True)
            return None

    def overlay(self, profiles):
        if not profiles:
            return profiles
        patterns = score_patterns()
        return overlay_profiles(profiles, self.repository.overlay_rows(list(profiles), patterns=patterns))

    def fit_scores(self, keys, preset):
        return self.repository.fit_scores(keys, preset, patterns=score_patterns())

    def ranked(self, vibe, limit=100):
        return self.repository.ranked(vibe, limit, patterns=score_patterns())
