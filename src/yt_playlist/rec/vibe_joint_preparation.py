"""One bounded off-request per-vibe fit, then guarded durable publication."""
import hashlib
import json
import time

from yt_playlist.core import fao, vibe_model_files as files
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import home_vibe, musical_preparation, musical_profiles, vibe_joint_model as model

VIBES = (*home_vibe.PRESETS, *INTENSITY_CHANNELS)
# Release qualification is deliberately empty until #819's frozen statistical,
# lifecycle and resource gates have independent evidence. Map configuration
# hashes to the accepted report hash; a successful fit alone cannot qualify it.
QUALIFICATIONS = {}
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
    return (value.get('revision') == (store.settings.get_setting(files.REVISION) or '0')
            and value.get('configuration') == config_identity())


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
                    for key, payload, _version, digest in musical_preparation.masked_recordings(reader, captured)}
        for row in batch:
            profile, digest = profiles[keys[row['recording']]]
            result.append({**row, 'metadata_prior': home_vibe.heuristic_affinity(profile, INTENSITY_CHANNELS.get(vibe, vibe))
                           if profile is not None else .5,
                           'metadata_profile_digest': digest, 'metadata_profile_version': musical_profiles.VERSION})
    return result


def step(store, *, seconds=10, memory_bytes=model.MEMORY_BYTES):
    """Synchronous rehearsal driver; the worker advances ``steps`` by turn."""
    work = steps(store, seconds=seconds, memory_bytes=memory_bytes)
    try:
        while True:
            next(work)
    except StopIteration as completed:
        return completed.value
    finally:
        work.close()


def steps(store, *, seconds=10, input_seconds=2, memory_bytes=model.MEMORY_BYTES):
    """Capture bounded metadata batches, then fit under its own admission.

    One guarded snapshot spans the batches. A yield holds only manifest inputs
    and the reader lease; no work-budget context or posterior survives a yield.
    Closing abandons unpublished work and releases its file/SQL leases.
    """
    if not qualified():
        return False
    now = time.time()
    for vibe in VIBES:
        previous = state(store, vibe)
        if current(store, previous) and (previous.get('outcome') != 'deferred'
                                        or now < previous.get('retry_at', 0)):
            continue
        staged = None
        revision = store.settings.get_setting(files.REVISION) or '0'
        configuration = config_identity()
        try:
            with store.read_snapshot() as snapshot:
                reader = snapshot.store
                revision = reader.settings.get_setting(files.REVISION) or '0'
                # Guard competing model publishers, including no prior head.
                reader.settings.get_setting(files.HEAD_PREFIX + vibe)
                with work_budget(seconds):
                    originals = reader.vibe_predictions.joint_inputs(vibe, model.MAX_ROWS)
                rows, input_started = [], time.monotonic()
                for start in range(0, len(originals), 32):
                    with work_budget(input_seconds):
                        rows.extend(prior_inputs(reader, originals[start:start + 32], vibe))
                    yield
                input_seconds_used = time.monotonic() - input_started
                fit_started = time.monotonic()
                with work_budget(seconds):
                    value = model.fit(rows, vibe, memory_bytes=memory_bytes)
                    generation, staged = reader.vibe_predictions.stage_joint_model(value) if value else ('', None)
                fit_seconds = time.monotonic() - fit_started
                refresh = _refresh_ready(reader, vibe)
                outcome = {'revision': revision, 'configuration': configuration,
                           'attempted_at': now,
                           'outcome': 'ready' if value else 'empty',
                           'wall_seconds': time.time() - now,
                           'input_seconds': input_seconds_used, 'fit_seconds': fit_seconds,
                           'recordings': len(rows),
                           'accounted_memory_bytes': value['diagnostics']['accounted_memory_bytes'] if value else 0}

                def publish():
                    store.vibe_predictions.publish_joint_model(vibe, generation, outcome,
                        files.measurement_ids(value) if value else [], staged, refresh)

                return snapshot.publish_ordered(publish)
        except (WorkBudgetExceeded, fao.FileBudgetExceeded, fao.FileChanged) as exc:
            attempts = previous.get('attempts', 0) + 1 if current(store, previous) else 1
            # No head is removed and no incomplete fit is recorded as ready.
            # A changed corpus/config is eligible immediately; otherwise retry
            # at a bounded cadence rather than making startup wait for a fit.
            outcome = {
                'revision': revision, 'configuration': configuration, 'outcome': 'deferred',
                'attempted_at': now,
                'reason': str(exc), 'attempts': attempts, 'retry_at': now + (60 if attempts < 3 else 900)}
            refresh = not current(store, previous) and _refresh_ready(store, vibe)
            store.vibe_predictions.defer_joint_model(vibe, outcome, refresh)
            return True
        finally:
            if staged:
                staged.close()
    return False


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
        if fitted['vibe'] != vibe or fitted['configuration'] != model.configuration():
            raise ValueError('Joint model conditioning/configuration differs from its head')
        return generation, model.Posterior(fitted)
    except (KeyError, TypeError, ValueError) as exc:
        raise fao.CorruptFile('Required joint vibe posterior is invalid') from exc
