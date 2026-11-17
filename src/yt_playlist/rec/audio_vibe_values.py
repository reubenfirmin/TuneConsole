"""Detached Vibe calculations from captured audio and specialist results.

The heuristic view is kept for offline comparisons; production presents
independent specialist results. Model loading belongs to the controller.
"""
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_arc
from yt_playlist.rec.home_vibe import PRESETS, axis_fit

# Persisted prediction/preparation compatibility; a module rename changes no semantics.
VERSION = 'audio-vibe-preview-7'
LABELS = {key: preset['label'] for key, preset in PRESETS.items()}
# Listening feedback describes the sound directly. Product preset names remain
# separate. Full descriptions accompany these attributes in the feedback UI.
ATTRIBUTE_LABELS = {key: preset['attributes'] for key, preset in PRESETS.items()}
MATCH_THRESHOLD = .65  # Engineering threshold, not a calibrated probability.


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


def heuristic(samples, *, metadata=None, require_complete=True):
    """Fit observed excerpts without loading models or application services."""
    if not samples or (require_complete and len(samples) < 3):
        return None
    # Actual song positions define the arc, including seeks and replacement
    # captures. API arrival order and scheduled point names do not define it.
    ordered = audio_vibe_arc.ordered(samples)
    features = [_features(sample) for sample in ordered]
    paths = {axis: audio_vibe_arc.path([row[axis] for row in features]) for axis in features[0]}
    fits = [{key: _excerpt_fit(key, row) for key in PRESETS} for row in features]
    ranked = []
    for key in PRESETS:
        excerpt_scores = [row[key] for row in fits]
        score, rule = audio_vibe_arc.fit(key, excerpt_scores)
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
    prediction = {'version': VERSION, 'arc_version': audio_vibe_arc.VERSION, 'taxonomy_version': TAXONOMY_VERSION,
            'vibe': matches[0]['vibe'],
            'vibes': [row['vibe'] for row in matches],
            'label': ' · '.join(row['label'] for row in matches),
            'selection_method': selection_method,
            'score_margin': round(ranked[0]['score'] - ranked[1]['score'], 5),
            'ranking': ranked, 'dimensions': paths, 'arc': arc, 'aggregation': 'ordered_arc',
            'evidence': evidence, 'sample_count': len(samples),
            'varying': varying, 'calibrated': False, 'recommendation_adoption': False}
    return prediction


def specialist_values(prediction):
    """Expose each purpose separately, never a cross-specialist winner."""
    fits = prediction['suitability']
    models = prediction['individual_models']
    ranking = [{'vibe': vibe, 'label': LABELS[vibe], 'score': fits[vibe],
                'excerpt_scores': [row['score'] for row in models[vibe]['excerpts']],
                'rule': models[vibe]['rule'], 'score_source': 'specialist'}
               for vibe in PRESETS if vibe in fits]
    return {**prediction, 'vibe': None, 'vibes': [], 'label': '',
            'ranking': ranking, 'score_margin': None, 'selection_method': 'independent_specialists',
            'evidence': ['Independent suitability scores; compare songs within a purpose, not purposes with each other',
                         'Observed excerpts retain their positions; gaps remain unobserved'],
            'unavailable': [vibe for vibe in PRESETS if vibe not in fits]}
