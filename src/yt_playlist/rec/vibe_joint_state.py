"""One bounded off-request per-vibe fit, then guarded durable publication."""
import hashlib
import json
import time

from yt_playlist.core import fao
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import musical_preparation, musical_profiles, vibe_joint_model as model, vibe_presets

VIBES = (*vibe_presets.PRESETS, *INTENSITY_CHANNELS)
# Configuration hashes map to accepted review receipt SHA256s. Qualification
# covers the frozen statistical, lifecycle, cold-start and resource gates:
# https://github.com/reubenfirmin/TuneConsole/issues/819#issuecomment-6704311492
LEGACY_SCORE_CONFIGURATION = '30f91e6a2d29d6e8816e49dd1778093c4bfe06c960b8c2e8648beb0be908b8b3'
QUALIFICATIONS = {
    LEGACY_SCORE_CONFIGURATION:
        '31fcdd9e2995df7b8c23d56c03c505fd3ecd38b9a323e99e48314bd2923c7a5e',
}
# Space out rebuilds of each channel while source or score work is queued.
BACKLOG_INTERVAL = 60


def config_identity():
    return hashlib.sha256(model.canonical(model.configuration())).hexdigest()


def qualified():
    return bool(QUALIFICATIONS) and config_identity() in QUALIFICATIONS


def state(store, vibe):
    return store.vibe_predictions.joint_state(vibe)[1]


def current(store, value):
    from yt_playlist.controllers.vibe_joint_preparation import current_values
    return current_values(value, store.vibe_predictions.joint_revision(), config_identity())


def due(store):
    return next_fit(store, waiting=False) is not None


def next_fit(store, *, waiting):
    from yt_playlist.controllers.vibe_joint_preparation import DISPATCH_VERSION, current_values
    from yt_playlist.rec.vibe_recording_rules import joint_policy

    if not qualified():
        return None
    now, configuration, policy = time.time(), config_identity(), joint_policy()
    revision = store.vibe_predictions.joint_revision()
    states = store.vibe_predictions.joint_fit_states()
    candidates = []
    for vibe in VIBES:
        value = states.get(vibe, {})
        if value.get('policy') != policy or value.get('configuration') != configuration:
            candidates.append((0, vibe))
            continue
        attempted = value.get('attempted_at', 0)
        if value.get('outcome') == 'deferred':
            earliest = value['retry_at']
        elif current_values(value, revision, configuration):
            if value.get('outcome') == 'ready' and value.get('dispatch_version') != DISPATCH_VERSION:
                candidates.append((0, vibe))
            continue
        else:
            earliest = attempted + BACKLOG_INTERVAL if waiting else 0
        if now >= earliest:
            candidates.append((attempted, vibe))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def admitted(store, *, waiting):
    return next_fit(store, waiting=waiting) is not None


def prior_inputs(reader, rows, vibe):
    """Attach the exact masked scalar covariate without dropping originals."""
    result = []
    for start in range(0, len(rows), 32):
        batch = rows[start:start + 32]
        keys, captured = reader.vibe_predictions.joint_profile_rows([row['recording'] for row in batch])
        profiles = {key: (json.loads(payload) if payload is not None else None, digest)
                    for key, payload, _version, digest in musical_preparation.masked_recordings(
                        captured, inputs=musical_preparation.load_masked_inputs(reader, captured))}
        for row in batch:
            profile, digest = profiles[keys[row['recording']]]
            result.append({**row, 'metadata_prior': vibe_presets.heuristic_affinity(profile, INTENSITY_CHANNELS.get(vibe, vibe))
                           if profile is not None else .5,
                           'metadata_profile_digest': digest, 'metadata_profile_version': musical_profiles.MASKED_VERSION})
    return result


def posterior(reader, vibe):
    """Serve the last compatible committed fit while newer evidence awaits fitting."""
    from yt_playlist.rec.vibe_recording_rules import joint_policy
    from yt_playlist.rec.vibe_score_versions import compatible_version, serving_prefix

    if not qualified():
        return None
    generation, value = reader.vibe_predictions.joint_state(vibe)
    if value.get('configuration') != config_identity() or value.get('policy') != joint_policy():
        return None
    if value.get('outcome') not in ('ready', 'deferred'):
        return None
    if not generation:
        if value.get('outcome') == 'ready':
            raise fao.CorruptFile('Ready joint vibe model has no head')
        return None
    try:
        fitted = reader.vibe_predictions.joint_model(generation)
        prefix = serving_prefix()
        if any(not compatible_version(row.get('version'), prefix=prefix)
               or not row['source'].startswith('audio_measurement/') for row in fitted['inputs']):
            return None
        if fitted['vibe'] != vibe or fitted['configuration'] != model.configuration():
            raise ValueError('Joint model conditioning/configuration differs from its head')
        return generation, model.Posterior(fitted)
    except (KeyError, TypeError, ValueError) as exc:
        raise fao.CorruptFile('Required joint vibe posterior is invalid') from exc
