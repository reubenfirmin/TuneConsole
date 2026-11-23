"""Public view of independent Vibe specialist scores."""
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_arc
from yt_playlist.rec.vibe_presets import PRESETS

# Existing recording measurements keep their lineage. Specialist arithmetic is unchanged.
VERSION = 'audio-vibe-preview-8'
MATCH_THRESHOLD = .65  # Provisional display cutoff on each independent purpose score.
LABELS = {key: preset['label'] for key, preset in PRESETS.items()}
ATTRIBUTE_LABELS = {key: preset['attributes'] for key, preset in PRESETS.items()}


def specialist_values(prediction, sample_count):
    """Expose each purpose separately, retaining excerpt evidence for inspection."""
    fits = prediction['suitability']
    models = prediction['individual_models']
    ranking = [{'vibe': vibe, 'label': LABELS[vibe], 'score': fits[vibe],
                'excerpt_scores': [row['score'] for row in models[vibe]['excerpts']],
                'rule': models[vibe]['rule'], 'score_source': 'specialist'}
               for vibe in PRESETS if vibe in fits]
    matches = [row['vibe'] for row in ranking if row['score'] >= MATCH_THRESHOLD]
    return {**prediction, 'version': VERSION, 'arc_version': audio_vibe_arc.VERSION,
            'taxonomy_version': TAXONOMY_VERSION, 'sample_count': sample_count,
            'aggregation': 'ordered_arc', 'calibrated': False, 'recommendation_adoption': False,
            'vibe': matches[0] if len(matches) == 1 else None, 'vibes': matches,
            'label': ' · '.join(LABELS[vibe] for vibe in matches), 'ranking': ranking, 'score_margin': None,
            'selection_method': 'independent_thresholds',
            'evidence': ['Scores compare songs within the same listening purpose',
                         'Scores cover the observed excerpts at their actual positions'],
            'unavailable': [vibe for vibe in PRESETS if vibe not in fits]}
