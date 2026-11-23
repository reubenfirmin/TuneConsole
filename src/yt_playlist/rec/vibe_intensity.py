"""Explicit activation/force ensemble, not a six-way classification probability."""
import math

VERSION = 'specialist-intensity-max-1'
CHANNELS = ('energize', 'release')
SOURCES = ('audio_measurement', 'artist', 'artist_genre')


def ensemble(fits):
    """Keep the existing strongest-axis policy with specialist inputs.

    Each component already follows its specialist's ordered-excerpt policy.
    Missing axes remain absent; the selected axis supplies its own support.
    This is a versioned engineering intensity scale, not calibrated enjoyment.
    """
    available = {}
    for name in CHANNELS:
        fit = fits.get(name)
        if not isinstance(fit, dict) or fit.get('source') not in SOURCES:
            continue
        score, support = fit.get('score'), fit.get('support')
        if (type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1
                and type(support) in (int, float) and math.isfinite(support) and 0 < support <= 1):
            available[name] = fit
    if not available:
        return None
    selected = max(available, key=lambda name: available[name]['score'])
    return {'score': available[selected]['score'], 'support': available[selected]['support'],
            'selected': selected,
            'source': 'specialist_ensemble', 'version': VERSION, 'components': available}
