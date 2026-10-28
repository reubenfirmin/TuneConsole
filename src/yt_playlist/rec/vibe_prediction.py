"""Conservative, nonrecursive predictions from distinct measured recordings.

Human annotations are INTERNAL DEVELOPMENT ONLY: fitting/evaluation targets,
never runtime observations, per-track overrides, or propagation seeds. Only
own provider/audio measurements enter these sufficient statistics.
"""
import json
from yt_playlist.core.audio_vibe_contract import PREDICTION_VERSION

VERSION = PREDICTION_VERSION
MIN_SUPPORT = .04
CAPS = {'artist_genre': .8, 'artist': .3, 'genre': .2}


def groups(artist, genre):
    """Exact canonical artist credit set and exact specific genre, no ancestors."""
    return ([json.dumps(['artist_genre', artist, genre], separators=(',', ':'))] if artist and genre else []) + (
        [json.dumps(['artist', artist, None], separators=(',', ':'))] if artist else []) + (
        [json.dumps(['genre', None, genre], separators=(',', ':'))] if genre else [])


def adjust(stats, evidence, sign):
    """One vote per recording, regardless of plays, videos or provider count."""
    result = {key: list(value) for key, value in stats.items()}
    for vibe, row in (evidence or {}).get('fits', {}).items():
        score, weight = row['score'], row['support']
        if weight <= 0:
            continue
        values = result.setdefault(vibe, [0, 0., 0., 0.])
        for index, amount in enumerate((1, weight, weight * score, weight * score * score)):
            values[index] += sign * amount
        if values[0] <= 0:
            result.pop(vibe)
    return result


def prediction(group_key, stats, vibe):
    count, weight, total, squared = stats.get(vibe, (0, 0., 0., 0.))
    if count <= 0 or weight <= 1e-9:
        return None
    mean = max(0., min(1., total / weight))
    variance = max(0., squared / weight - mean * mean)
    kind = json.loads(group_key)[0]
    agreement = max(.1, 1 - 4 * variance)
    support = CAPS[kind] * weight / (weight + 1) * agreement
    if support < MIN_SUPPORT:
        return None
    return {'score': mean, 'support': support, 'source': kind,
            'recordings': count, 'variance': variance}


def resolve(fallback, own, candidates):
    result = {}
    for vibe, baseline in fallback.items():
        measured = (own or {}).get('fits', {}).get(vibe)
        if measured:
            result[vibe] = {**measured, 'source': own['source']}
            continue
        # Prefer the most specific applicable cohort. Correlated artist and
        # genre cohorts are never added as independent corroborating votes.
        estimate = next((row for key, stats in candidates
                         if (row := prediction(key, stats, vibe)) is not None), None)
        result[vibe] = ({**estimate, 'score': baseline + estimate['support'] * (estimate['score'] - baseline)}
                        if estimate else {'score': baseline, 'support': 0., 'source': 'heuristic'})
    return {'version': VERSION, 'fits': result}
