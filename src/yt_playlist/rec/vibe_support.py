"""Evidence required for a prepared Vibe fit to count toward menu readiness."""
import math

from yt_playlist.rec import musical_character, vibe_preparation


def supported(track, preset):
    profile = track.get('musical') or {}
    fit = (profile.get('vibes', {}).get('fits') or {}).get(preset) or {}
    support, score = fit.get('support'), fit.get('score')
    return (profile.get('version') == musical_character.VERSION
            and vibe_preparation.compatible_version(profile.get('vibes', {}).get('inputs_version'))
            and fit.get('source') not in (None, 'heuristic')
            and type(support) in (int, float) and math.isfinite(support) and support > 0
            and type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1)
