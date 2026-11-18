"""Capture bounded recording facts and load one posterior at a time."""
import json

from yt_playlist.core import fao
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import home_vibe, vibe_recording_rules as values


class RecordingPreparation:
    def __init__(self, predictions, sources, genres, *, prediction, version, qualified, masked,
                 posterior, current_empty):
        self.predictions, self.sources, self.genres = predictions, sources, genres
        self.prediction, self.version, self.qualified = prediction, version, qualified
        self.masked, self.posterior, self.current_empty = masked, posterior, current_empty

    def steps(self, rows, musical, *, source_inputs=None):
        keys = [row[0] for row in rows]
        inputs = self.predictions.inputs(keys)
        if source_inputs is None:
            source_inputs = self.sources.recording_inputs(keys)
        stamp = self.version()
        predictor = self.prediction() if any(value['samples'] for value in inputs.values()) else None
        decoded = [json.loads(raw) for _key, _revision, raw, _digest, _different in rows]
        labels = {features.get('genre') for payload in decoded
                  for features in (payload.get('library'), payload.get('discovered')) if features is not None}
        labels.update(value['metadata'].get('genre') for value in inputs.values() if value.get('metadata'))
        labels.update(overrides.get('genre') for _sources, overrides, _truncated in source_inputs.values())
        axes = self.genres.feature_axes(labels)
        tracks, changed = values.recording_values(rows, inputs, source_inputs, axes, predictor, stamp)
        cohorts = self.predictions.groups(sorted(values.cohort_keys(tracks, musical)))
        touched = values.cohort_values(cohorts, changed)
        retained = self.predictions.retained_joint_fits(keys)
        qualified = self.qualified()
        albums = self.predictions.joint_albums(keys) if qualified else {}
        masked = {key: json.loads(payload) if payload is not None else None
                  for key, payload, _version, _digest in self.masked(rows)} if qualified else {}
        usable = {key for key, payload, _version, _digest in musical if payload is not None}
        recording_ids = {key: value['recording'] for key, value in inputs.items()}
        pooled, repairs = {}, set()
        for vibe in (*home_vibe.PRESETS, *INTENSITY_CHANNELS):
            eligible = [key for key in usable if not tracks[key]['evidence'] or vibe not in tracks[key]['evidence']['fits']]
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
            musical, tracks, cohorts, pooled, retained, albums, masked, empty, stamp)
        if self.version() != stamp:
            raise WorkBudgetExceeded('Configured vibe model changed during capture')
        return updated, (tracks, resolved, sorted(touched | repairs))
