"""One bounded off-request per-vibe fit, then guarded durable publication."""
import hashlib
import json
import time

from yt_playlist.core import fao, vibe_model_files as files
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS, serving_compatible
from yt_playlist.rec import home_vibe, musical_preparation, musical_profiles, vibe_joint_model as model

VIBES = (*home_vibe.PRESETS, *INTENSITY_CHANNELS)
# Configuration hashes map to accepted review receipt SHA256s. Qualification
# covers the frozen statistical, lifecycle, cold-start and resource gates:
# https://github.com/reubenfirmin/TuneConsole/issues/819#issuecomment-6704311492
QUALIFICATIONS = {
    '30f91e6a2d29d6e8816e49dd1778093c4bfe06c960b8c2e8648beb0be908b8b3':
        '31fcdd9e2995df7b8c23d56c03c505fd3ecd38b9a323e99e48314bd2923c7a5e',
}
# Under continuing upstream work, finish one bounded eight-channel cycle and
# allow upstream capture to progress before admitting another cycle.
BACKLOG_INTERVAL = 60


def config_identity():
    return hashlib.sha256(model.canonical(model.configuration())).hexdigest()


def qualified():
    return bool(QUALIFICATIONS) and config_identity() in QUALIFICATIONS


def state(store, vibe):
    return json.loads(store.settings.get_setting(files.STATE_PREFIX + vibe) or '{}')


def current(store, value):
    from yt_playlist.controllers.vibe_joint_preparation import current_values
    return current_values(value, store.settings.get_setting(files.REVISION) or '0', config_identity())


def due(store):
    return qualified() and any(not current(store, value := state(store, vibe))
               or value.get('outcome') == 'deferred' and time.time() >= value.get('retry_at', 0)
               for vibe in VIBES)


def active(store):
    """Continue a partially published revision before changing its originals."""
    if not qualified():
        return False
    states = [state(store, vibe) for vibe in VIBES]
    published = [value for value in states if current(store, value)]
    attempted = max((value.get('attempted_at', 0) for value in published), default=0)
    # A stream of stale publication guards must eventually return the worker
    # to ordinary fair source turns, even when no further model can publish.
    return (0 < len(published) < len(states)
            and time.time() < attempted + len(VIBES) * 10)


def scoring(store):
    """Give a completed model cycle bounded time to publish useful scores.

    Current generation references are the durable acknowledgement, so restarting
    the worker between the last fit and score publication cannot lose this turn.
    A corpus with no eligible targets eventually returns to source work.
    """
    if not qualified():
        return False
    states = [state(store, vibe) for vibe in VIBES]
    if (not all(current(store, value) and value.get('outcome') in ('ready', 'empty', 'deferred') for value in states)
            or time.time() >= max(value.get('attempted_at', 0) for value in states) + len(VIBES) * 10):
        return False
    generations = [store.settings.get_setting(files.HEAD_PREFIX + vibe)
                   for vibe, value in zip(VIBES, states) if value['outcome'] == 'ready']
    try:
        for generation in generations:
            files.relative(generation)
    except ValueError:
        return False  # Ordinary recording preparation owns missing-head repair.
    return bool(generations) and not store.vibe_predictions.has_joint_scores(generations)


def admitted(store, *, waiting):
    if not due(store):
        return False
    if not waiting or active(store):
        return True
    attempted = max(state(store, vibe).get('attempted_at', 0) for vibe in VIBES)
    return time.time() >= attempted + BACKLOG_INTERVAL


def _refresh_ready(store, vibe):
    # Coalesce one score pass after this revision's model turns have settled,
    # including channels that deferred rather than published a replacement.
    return all(other == vibe or current(store, state(store, other)) for other in VIBES)


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
            result.append({**row, 'metadata_prior': home_vibe.heuristic_affinity(profile, INTENSITY_CHANNELS.get(vibe, vibe))
                           if profile is not None else .5,
                           'metadata_profile_digest': digest, 'metadata_profile_version': musical_profiles.VERSION})
    return result


def posterior(reader, vibe):
    """Return a current model only. Stale scores retain their actual generation."""
    if not qualified():
        return None
    value = state(reader, vibe)
    if not current(reader, value) or value.get('outcome') != 'ready':
        return None
    generation = reader.settings.get_setting(files.HEAD_PREFIX + vibe)
    if not generation:
        raise fao.CorruptFile('Ready joint vibe model has no head')
    try:
        fitted = reader.vibe_predictions.joint_model(generation)
        if any(not serving_compatible(row.get('version')) for row in fitted['inputs']):
            return None
        if fitted['vibe'] != vibe or fitted['configuration'] != model.configuration():
            raise ValueError('Joint model conditioning/configuration differs from its head')
        return generation, model.Posterior(fitted)
    except (KeyError, TypeError, ValueError) as exc:
        raise fao.CorruptFile('Required joint vibe posterior is invalid') from exc
