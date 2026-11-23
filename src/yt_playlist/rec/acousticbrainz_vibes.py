"""Estimate specialist Vibe scores from verified AcousticBrainz classifiers."""
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

from yt_playlist.core.audio_vibe_contract import ACOUSTICBRAINZ_ESTIMATE as SOURCE
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec import audio_vibe_specialists

INTERVAL = 86400
VIBES = tuple(vibe for vibe, _module in audio_vibe_specialists.SPECIALISTS)
FIELDS = ('danceability', 'mood_party', 'mood_aggressive', 'mood_happy', 'mood_sad',
          'mood_relaxed', 'mood_acoustic', 'instrumental')
ARTIFACT = Path(__file__).with_name('data') / 'acousticbrainz-vibes.json'


def input_values(sources, mbid):
    """Accept the complete input used in the experiment, with native provenance."""
    for source in sources:
        if ((source['provider'], source['scope']) != ('acousticbrainz', 'recording')
                or str(source['entity_id']).lower() != mbid):
            continue
        values = []
        for field in FIELDS:
            observation = source['observations'].get(field) or {}
            provenance = observation.get('provenance') or {}
            value = observation.get('value')
            if (provenance.get('scope') != 'recording' or provenance.get('unit') != 'classifier_probability'
                    or str(provenance.get('entity_id')).lower() != mbid or type(value) not in (int, float)
                    or not 0 <= value <= 1):
                break
            values.append(value)
        if len(values) == len(FIELDS):
            return values
    return None


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def valid_coefficients(coefficients):
    return (isinstance(coefficients, dict) and set(coefficients) == set(VIBES)
            and all(isinstance(row, list) and len(row) == len(FIELDS) + 1
                    and all(type(value) in (float, int) and math.isfinite(value) for value in row)
                    for row in coefficients.values()))


@lru_cache(maxsize=1)
def bundled():
    artifact = json.loads(ARTIFACT.read_text())
    if (artifact['format'] != 1 or tuple(artifact['fields']) != FIELDS
            or not valid_coefficients(artifact['coefficients'])
            or any(len(artifact[field]) != len(FIELDS) or not all(map(math.isfinite, artifact[field]))
                   for field in ('mean', 'scale'))
            or min(artifact['scale']) <= 0 or artifact['local_regularization'] <= 0):
        raise ValueError('Invalid bundled AcousticBrainz Vibe mapping')
    return {**artifact, 'identity': fingerprint(artifact)}


def measurement_version():
    from yt_playlist.rec import vibe_intensity
    from yt_playlist.rec.vibe_score_versions import serving_prefix

    artifact = bundled()
    return serving_prefix() + '/'.join((artifact['arc_version'], vibe_intensity.VERSION, artifact['specialists']))


def saved_model(raw):
    try:
        state = json.loads(raw) if raw else {}
        if (state.get('seed') == bundled()['identity'] and math.isfinite(state.get('checked_at'))
                and valid_coefficients(state.get('coefficients'))):
            return state
    except (ValueError, AttributeError, TypeError):
        pass
    return {}


def coefficients(state):
    return state.get('coefficients', bundled()['coefficients'])


def version_prefix():
    return 'acousticbrainz/' + bundled()['identity'] + '/'


def estimate_prefix():
    return measurement_version() + '/' + version_prefix()


def estimate_version(model):
    return estimate_prefix() + fingerprint(model)


def score(values, model):
    seed = bundled()
    vector = [(value - mean) / scale for value, mean, scale in zip(values, seed['mean'], seed['scale'], strict=True)]
    return {vibe: min(1., max(0., row[0] + math.fsum(weight * value for weight, value in zip(row[1:], vector, strict=True))))
            for vibe, row in model.items()}


def adjusted(seed, examples, *, strength=None):
    """Fit corrections to the whole bundled model using only direct measurements."""
    import numpy as np
    from yt_playlist.core.numerics import single_threaded

    if not examples:
        return seed['coefficients']
    scale, mean = np.array(seed['scale']), np.array(seed['mean'])
    inputs = (np.array([example[0] for example in examples]) - mean) / scale
    design = np.column_stack((np.ones(len(inputs)), inputs))
    penalty = np.eye(design.shape[1]) * (seed['local_regularization'] if strength is None else strength)
    result = {}
    with single_threaded():
        for vibe, start in seed['coefficients'].items():
            labels = [(index, fits[vibe]) for index, (_values, fits) in enumerate(examples) if vibe in fits]
            if labels:
                indexes, observed = zip(*labels, strict=True)
                target, support = np.array(observed).T
                x = design[list(indexes)]
                result[vibe] = np.linalg.solve(x.T @ (support[:, None] * x) + penalty,
                                              x.T @ (support * target) + penalty @ start).tolist()
            else:
                result[vibe] = start
    return result
