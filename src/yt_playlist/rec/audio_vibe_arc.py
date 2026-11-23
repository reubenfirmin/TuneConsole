"""The single temporal policy for sampled-song Vibe scoring.

Only observed excerpts contribute. Classifiers and their within-excerpt source
mappings remain independent of this policy.
"""
import math
from statistics import median_high

VERSION = 'ordered-vibe-arc-1'
FADE_PENALTY = .25


def ordered(samples):
    """Arrival order and nominal capture names never define the song's arc."""
    return sorted(samples, key=lambda sample: (
        sample['position_seconds'], sample['target_seconds'], sample['id']))


def fit(vibe, values):
    """Apply the existing arc heuristic after per-excerpt inference and mapping.

    Calm/unobtrusive/easygoing fits retain the least suitable observation;
    cathartic force retains its observed peak. Momentum and positivity can be
    sustained or build, while a fade reduces their fit. These policies do not
    imply that every vibe must change under every reversal.
    """
    if not values or any(type(value) not in (int, float) or not math.isfinite(value)
                         or not 0 <= value <= 1 for value in values):
        raise ValueError('Invalid audio-vibe arc scores')
    if vibe in ('relax', 'concentrate', 'cruise'):
        return min(values), 'weakest_excerpt'
    if vibe == 'release':
        return max(values), 'peak_excerpt'
    if vibe not in ('energize', 'uplift'):
        raise ValueError('Unknown audio-vibe arc')
    fading = max(0, values[0] - values[-1])
    return max(0, max(median_high(values), values[-1]) - FADE_PENALTY * fading), 'sustained_or_building'


def specialist(samples, model, scorer, vibe):
    """Keep each classifier's native evidence intact until temporal scoring."""
    excerpts = []
    for sample in ordered(samples):
        result = scorer(model, [sample['embedding']['values']])
        excerpts.append({**result, 'sample_id': sample['id'],
                         'position_seconds': sample['position_seconds'],
                         'target_seconds': sample['target_seconds'],
                         'analysed_seconds': sample.get('analysed_seconds'),
                         'capture_reason': sample.get('capture_reason', 'scheduled')})
    score, rule = fit(vibe, [excerpt['score'] for excerpt in excerpts])
    return {'score': score, 'sample_count': len(excerpts), 'excerpts': excerpts,
            'aggregation': 'ordered_arc', 'arc_version': VERSION, 'rule': rule,
            'sha256': model['sha256'], 'score_semantics': 'independent_uncalibrated_suitability'}
