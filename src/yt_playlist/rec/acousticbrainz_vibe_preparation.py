"""Refit the listener's AcousticBrainz adjustment on the preparation worker."""
import json
import time
from itertools import groupby

from yt_playlist.rec import acousticbrainz_vibes as model


def _checked(raw, now):
    state = model.saved_model(raw)
    return state, now < state.get('checked_at', float('-inf')) + model.INTERVAL


def due(store):
    return not _checked(store.vibe_predictions.acousticbrainz_model(), time.time())[1]


def step(store):
    now = time.time()
    raw = store.vibe_predictions.acousticbrainz_model()
    if _checked(raw, now)[1]:
        return False
    with store.read_snapshot() as captured:
        reader = captured.store
        raw = reader.vibe_predictions.acousticbrainz_model()
        previous, checked = _checked(raw, now)
        if checked:
            return False
        rows = reader.vibe_predictions.acousticbrainz_training_rows(model.measurement_version())
        revision = model.fingerprint(rows)
        active = model.coefficients(previous)
        coefficients = active
        if revision != previous.get('inputs'):
            examples = {}
            for recording, classifiers, vibe, score, support in rows:
                if recording not in examples:
                    examples[recording] = (json.loads(classifiers), {})
                examples[recording][1][vibe] = score, support
            coefficients = model.adjusted(model.bundled(), list(examples.values()))
        state = json.dumps({'seed': model.bundled()['identity'], 'checked_at': now,
                            'inputs': revision, 'coefficients': coefficients},
                           sort_keys=True, separators=(',', ':'), allow_nan=False)
        # A competing model publication must win. Other evidence changes belong
        # to the next day, as the stored digest describes this snapshot exactly.
        installed = False

        def publish():
            nonlocal installed
            installed = store.vibe_predictions.install_acousticbrainz_model(
                raw, state, refresh=coefficients != active)

        return captured.publish_ordered(publish, validate_inputs=False) and installed


def rescore(store, limit):
    if store.vibe_predictions.acousticbrainz_pending() is None:
        return False
    with store.read_snapshot() as captured:
        reader = captured.store
        pending = reader.vibe_predictions.acousticbrainz_pending()
        if pending is None:
            return False
        raw = reader.vibe_predictions.acousticbrainz_model()
        coefficients = model.coefficients(model.saved_model(raw))
        version = model.estimate_version(coefficients)
        rows = reader.vibe_predictions.acousticbrainz_estimate_page(pending['cursor'], limit)
        updates, count, last = [], 0, None
        for (recording, classifiers), fits in groupby(rows, key=lambda row: (row[0], row[1])):
            count += 1
            last = recording
            stale = {vibe: coefficients[vibe] for _, _, vibe, previous in fits if previous != version}
            if stale:
                scores = model.score(json.loads(classifiers), stale)
                updates.extend((score, version, recording, vibe) for vibe, score in scores.items())
        cursor = last if count == limit else None
        return captured.publish(lambda: store.vibe_predictions.publish_acousticbrainz_page(updates, cursor))
