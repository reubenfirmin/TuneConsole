"""Capture bounded recording facts and load one posterior at a time."""
import json
import logging
import time
from functools import cache

from yt_playlist.core import fao
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import home_vibe, vibe_recording_rules as values
from yt_playlist.rec.vibe_joint_state import config_identity
from yt_playlist.rec.vibe_score_policy import joint_suffix

log = logging.getLogger(__name__)


class RecordingPreparation:
    def __init__(self, predictions, sources, genres, *, prediction, version, qualified, masked,
                 posterior, current_empty, acousticbrainz_model):
        self.predictions, self.sources, self.genres = predictions, sources, genres
        self.prediction, self.version, self.qualified = prediction, version, qualified
        self.masked, self.posterior, self.current_empty = masked, posterior, current_empty
        self.acousticbrainz_model = acousticbrainz_model

    def steps(self, rows, musical, *, source_inputs=None):
        input_started = time.monotonic()
        keys = [row[0] for row in rows]
        stamp = self.version()
        predictor = cache(self.prediction)
        inputs = self.predictions.inputs(keys, usable_samples=lambda samples: predictor().usable_samples(samples))
        if source_inputs is None:
            source_inputs = self.sources.recording_inputs(keys)
        configuration = config_identity()
        decoded = [json.loads(raw) for _key, _revision, raw, _digest, _different in rows]
        labels = {features.get('genre') for payload in decoded
                  for features in (payload.get('library'), payload.get('discovered')) if features is not None}
        labels.update(value['metadata'].get('genre') for value in inputs.values() if value.get('metadata'))
        labels.update(overrides.get('genre') for _sources, overrides, _truncated in source_inputs.values())
        axes = self.genres.feature_axes(labels)
        candidates = [key for key, (sources, _overrides, _truncated) in source_inputs.items()
                      if any(source['provider'] == 'acousticbrainz' and source['scope'] == 'recording'
                             for source in sources)]
        matches = self.predictions.acousticbrainz_matches(candidates) if candidates else {}
        model = self.acousticbrainz_model() if matches else None
        tracks = values.recording_values(rows, inputs, source_inputs, axes,
                                         predictor() if any(value['samples'] for value in inputs.values()) else None, stamp,
                                         matches=matches, model=model)
        retained = self.predictions.retained_joint_fits(keys)
        needs_conditioning = self.qualified() or any(retained.values())
        albums = self.predictions.joint_albums(keys) if needs_conditioning else {}
        masked = {key: json.loads(payload) if payload is not None else None
                  for key, payload, _version, _digest in self.masked(rows)} if needs_conditioning else {}
        usable = {key for key, payload, _version, _digest in musical if payload is not None and tracks[key]}
        recording_ids = {key: value['recording'] for key, value in inputs.items()}
        score_started = time.monotonic()
        pooled, repairs = {}, set()
        for vibe in (*home_vibe.PRESETS, *INTENSITY_CHANNELS):
            eligible = [key for key in usable if vibe not in tracks[key].get('acousticbrainz', {})
                        and (not tracks[key]['evidence'] or vibe not in tracks[key]['evidence']['fits'])]
            pooled[vibe] = None
            if not eligible:
                continue
            loaded = False
            try:
                prepared = self.posterior(vibe)
                if prepared:
                    generation, posterior = prepared
                    pooled[vibe] = generation, values.posterior_values(
                        posterior, eligible, recording_ids, tracks, albums, masked, vibe)
                    del posterior
                    loaded = True
                del prepared
            except fao.CorruptFile:
                repairs.add('!joint:' + vibe)
            if loaded:
                # Release the dense model before yielding to the worker budget.
                yield
        empty = {vibe for vibe in {vibe for fits in retained.values() for vibe in fits}
                 if pooled.get(vibe) is None and self.current_empty(vibe)}
        updated, resolved = values.resolved_values(
            musical, tracks, pooled, retained, albums, masked, empty, stamp, joint_suffix(configuration),
            self.predictions.retained_conditioning)
        if self.version() != stamp or config_identity() != configuration:
            raise WorkBudgetExceeded('Configured vibe model changed during capture')
        log.debug('Vibe recording batch recordings=%d input_seconds=%.6f score_seconds=%.6f measured=%d predictions=%d',
            len(keys), score_started - input_started, time.monotonic() - score_started,
            sum(bool(track and track['evidence']) for track in tracks.values()),
            sum('generation' in fit for fits in resolved.values() for fit in fits.values()))
        return updated, (tracks, resolved, sorted(repairs))
