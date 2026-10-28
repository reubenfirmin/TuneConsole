"""Independent, uncalibrated fits from captured audio and selected metadata.

Used by the development banner and background production vibe preparation.
Human annotations remain INTERNAL DEVELOPMENT ONLY; this module never reads
them or turns a listener's annotation into a track override. An explicitly
configured JSON artifact supplies coefficients, not saved per-song judgments.
"""
from statistics import median_high

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec.home_vibe import PRESETS, axis_fit

VERSION = 'audio-vibe-preview-6'
LABELS = {key: preset['label'] for key, preset in PRESETS.items()}
# Listening feedback describes the sound directly. Product preset names remain
# separate. Full descriptions accompany these attributes in the feedback UI.
ATTRIBUTE_LABELS = {key: preset['attributes'] for key, preset in PRESETS.items()}
MATCH_THRESHOLD = .65  # Engineering threshold, not a calibrated probability.
ARC_CHANGE = .1
FADE_PENALTY = .25


def _features(sample):
    tags = {key.lower(): value for key, value in sample['tags'].items()}
    mood = {key: value['score'] for key, value in sample['traits'].items()}
    return {
        'relaxed': mood['relaxed'], 'aggression': mood['aggressive'],
        'happiness': mood['happy'], 'sadness': mood['sad'],
        'intensity': max(0, min(1, (mood['arousal'] - 1) / 8)),
        # A weak dance-tag proxy until listening labels validate a groove model.
        # House, metal and other genres must not supply or suppress groove.
        'groove': tags.get('dance', .5),
        'emotional_tone': max(0, min(1, (mood['valence'] - 1) / 8)),
    }


def _excerpt_fit(key, values):
    """Combine axes within one excerpt, never across the three excerpts."""
    weights = PRESETS[key]['weights']
    return sum(abs(weight) * axis_fit(key, axis, values.get(axis))
               for axis, weight in weights.items()) / sum(abs(w) for w in weights.values())


def _path(values):
    steps = [later - earlier for earlier, later in zip(values, values[1:])]
    span = max(values) - min(values)
    if span < ARC_CHANGE:
        shape = 'steady'
    elif any(step > ARC_CHANGE for step in steps) and any(step < -ARC_CHANGE for step in steps):
        shape = 'peak' if values.index(max(values)) not in (0, len(values) - 1) else 'dip'
    elif values[-1] - values[0] >= ARC_CHANGE:
        shape = 'rising'
    elif values[0] - values[-1] >= ARC_CHANGE:
        shape = 'falling'
    else:
        shape = 'changing'
    return {'values': values, 'minimum': min(values), 'maximum': max(values), 'range': span,
            'steps': steps, 'net_change': values[-1] - values[0], 'shape': shape}


def _arc_fit(key, fits):
    # Provisional listening-purpose rules, not learned/calibrated song traits.
    # Calm and unobtrusive listening must survive the least suitable excerpt;
    # a quiet introduction cannot cancel a later forceful passage into Cruise.
    if key in ('relax', 'concentrate', 'cruise'):
        return min(fits), 'weakest_excerpt'
    if key == 'release':
        return max(fits), 'peak_excerpt'
    # Energy and emotional lift can be sustained or build toward the later
    # excerpt. An isolated middle burst is different from a sustained/building
    # arc, and reversing the arc can reduce the fit. No excerpt mean is used.
    fading = max(0, fits[0] - fits[-1])
    return max(0, max(median_high(fits), fits[-1]) - FADE_PENALTY * fading), 'sustained_or_building'


def predict(samples, *, use_model=True, metadata=None, require_complete=True):
    """Fit observed excerpts; offline training can also use partial track coverage."""
    if not samples or (require_complete and len(samples) < 3):
        return None
    # Actual song positions define the arc, including seeks and replacement
    # captures. API arrival order and scheduled point names do not define it.
    ordered = sorted(samples, key=lambda sample: (
        sample['position_seconds'], sample['target_seconds'], sample['id']))
    features = [_features(sample) for sample in ordered]
    paths = {axis: _path([row[axis] for row in features]) for axis in features[0]}
    fits = [{key: _excerpt_fit(key, row) for key in PRESETS} for row in features]
    ranked = []
    for key in PRESETS:
        excerpt_scores = [row[key] for row in fits]
        score, rule = _arc_fit(key, excerpt_scores)
        ranked.append({'vibe': key, 'label': LABELS[key], 'score': round(score, 5),
                       'excerpt_scores': excerpt_scores, 'rule': rule})
    ranked.sort(key=lambda row: row['score'], reverse=True)
    matches = [row for row in ranked if row['score'] >= MATCH_THRESHOLD]
    selection_method = 'strong_fit' if matches else 'best_available'
    # Every complete song gets a hypothesis for the listener to correct. A
    # weak lead is retained as such, rather than becoming an abstention.
    if not matches:
        matches = ranked[:1]

    def describe(label, axis):
        path = paths[axis]
        return f"{label}: " + ', '.join(f'{value:.2f}' for value in path['values']) + f" ({path['shape']})"

    evidence = [
        'Sample positions: ' + ', '.join(f"{sample['position_seconds']:.1f}s" for sample in ordered),
        describe('Energy signal', 'intensity'),
        describe('Tension / force signal', 'aggression'),
        describe('Relaxed signal', 'relaxed'),
        describe('Dance-tag proxy', 'groove') + ' (groove is not yet validated)',
        describe('Positive mood signal', 'happiness'),
        describe('Sad signal', 'sadness'),
        'Fits use the ordered excerpts, not their average; intervals between excerpts remain unobserved',
        'Focus is a coarse proxy: vocal presence, steadiness and distraction are not established by these models',
    ]
    if selection_method == 'best_available':
        evidence.append('Showing the strongest available fit below the provisional cutoff; listening feedback is needed')
    varying = any(path['range'] >= .5 for path in paths.values())
    arc = {'order': 'position_seconds', 'scope': 'sampled_excerpts', 'excerpts': [
        {'sample_id': sample['id'], 'point': sample['point'], 'position_seconds': sample['position_seconds'],
         'target_seconds': sample['target_seconds'], 'duration_seconds': sample['duration_seconds'],
         'capture_reason': sample.get('capture_reason', 'scheduled'), 'dimensions': values, 'fits': scores}
        for sample, values, scores in zip(ordered, features, fits)],
        'transitions': [
            {'from_sample_id': earlier['id'], 'to_sample_id': later['id'],
             'gap_seconds': later['position_seconds'] - earlier['position_seconds'],
             'changes': {axis: after[axis] - before[axis] for axis in paths}}
            for earlier, later, before, after in zip(ordered, ordered[1:], features, features[1:])]}
    prediction = {'version': VERSION, 'taxonomy_version': TAXONOMY_VERSION,
            'vibe': matches[0]['vibe'],
            'vibes': [row['vibe'] for row in matches],
            'label': ' · '.join(row['label'] for row in matches),
            'selection_method': selection_method,
            'score_margin': round(ranked[0]['score'] - ranked[1]['score'], 5),
            'ranking': ranked, 'dimensions': paths, 'arc': arc, 'aggregation': 'ordered_arc',
            'evidence': evidence, 'sample_count': len(samples),
            'varying': varying, 'calibrated': False, 'recommendation_adoption': False}
    if use_model:
        from yt_playlist.rec.audio_vibe_model import apply
        return apply(samples, prediction, metadata)
    return prediction
