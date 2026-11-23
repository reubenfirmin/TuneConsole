"""Evidence required for a prepared Vibe fit to count toward menu readiness."""
import math

from yt_playlist.rec import musical_character
from yt_playlist.rec.vibe_score_policy import compatible_score, score_patterns


def supported(track, preset, *, patterns=None):
    profile = track.get('musical') or {}
    vibes = profile.get('vibes') or {}
    fit = (vibes.get('fits') or {}).get(preset) or {}
    support, score = fit.get('support'), fit.get('score')
    return (profile.get('version') == musical_character.VERSION
            and fit.get('source') not in (None, 'heuristic')
            and compatible_score(fit.get('inputs_version', vibes.get('inputs_version')), fit['source'],
                                 score_patterns() if patterns is None else patterns)
            and type(support) in (int, float) and math.isfinite(support) and support > 0
            and type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1)
