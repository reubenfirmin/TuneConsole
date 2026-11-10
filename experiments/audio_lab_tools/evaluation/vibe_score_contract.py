"""Executable independent-score boundary; experimental, not a serving adapter."""
import math

VIBES = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')


def unit(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def coverage(intervals, expected_interval):
    """Count only real, distinct, nonoverlapping seconds inside the target unit."""
    left, right = expected_interval
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (left, right)) or left >= right:
        raise ValueError('Invalid annotation interval')
    observed = sorted(set(tuple(pair) for pair in intervals))
    end, seconds = left, 0.
    for start, stop in observed:
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, stop)) or not left <= start < stop <= right:
            raise ValueError('Capture falls outside its annotation interval')
        seconds += max(0., stop - max(start, end))
        end = max(end, stop)
    return {'intervals': [list(pair) for pair in observed], 'distinct_excerpts': len(observed),
            'seconds': seconds, 'expected_interval': [left, right],
            'complete_annotation_interval': math.isclose(seconds, right - left, abs_tol=1e-8),
            'whole_recording': False}


def measurement(value, *, intervals, expected_interval, model, calibrator, domain,
                domain_supported=False, threshold=None):
    if value is not None and not unit(value):
        raise ValueError('Independent score must be finite and bounded')
    if not all(isinstance(v, str) and v for v in (model, calibrator, domain)):
        raise ValueError('Missing score provenance')
    if threshold is not None and (not isinstance(threshold, dict) or not unit(threshold.get('value'))
                                  or not isinstance(threshold.get('evidence'), str) or not threshold['evidence']):
        raise ValueError('Threshold requires its own evidence identity')
    captured = coverage(intervals, expected_interval)
    if not captured['seconds'] and value is not None:
        raise ValueError('No numeric measurement without audio')
    reason = ('missing_score' if value is None else 'unsupported_domain' if not domain_supported
              else 'partial_capture' if not captured['complete_annotation_interval']
              else 'unvalidated_threshold' if threshold is None else None)
    return {'score': value, 'semantics': 'independent_mapped_rating', 'model': model,
            'calibrator': calibrator, 'domain': domain, 'coverage': captured,
            'ranking_allowed': value is not None and domain_supported,
            'strong_match': value >= threshold['value'] if reason is None else None,
            'threshold': threshold, 'abstention': reason}


def assemble(shared_scores, independent):
    """Keep the legacy classifier; independent purpose fits never compete by max."""
    if set(shared_scores) != set(VIBES) or not all(unit(v) for v in shared_scores.values()):
        raise ValueError('Invalid legacy scores')
    if not set(independent) <= set(VIBES):
        raise ValueError('Unknown listening purpose')
    return {'shared_scores': dict(shared_scores),
            'shared_top_vibe': max(VIBES, key=shared_scores.__getitem__),
            'independent': dict(independent),
            'strong_vibes': [vibe for vibe in VIBES if independent.get(vibe, {}).get('strong_match') is True]}
