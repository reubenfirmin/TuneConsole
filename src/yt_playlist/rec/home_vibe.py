"""Temporary, explicit sound preferences for Home mixes, applied before track selection."""
import math

from yt_playlist.rec import mix_tuning, musical_character
from yt_playlist.rec.ordering import _field

SETTING = "home_mix_vibe"
VERSION = 'musical-vibes-5'
FIT_STEP = .04

# Listening purposes, with provisional cross-genre sound preferences. The full
# descriptions also define the internal model-development labeling task; those
# raw annotations are not ranking inputs or track overrides. Genre is never a target.
PRESETS = {
    "relax": {"label": "Relax", "icon": "feather", "color": "mint",
              "attributes": "Calm, soothing, unhurried",
              "description": "Helps me settle down and release tension.",
              "signals": "Calm, soothing, unhurried. Helps me settle down and release tension.",
              "weights": {"relaxed": 3, "intensity": -2, "aggression": -1}},
    "concentrate": {"label": "Focus", "icon": "orbit", "color": "blue",
                    "attributes": "Steady, absorbing, unobtrusive",
                    "description": "Supports concentration without repeatedly grabbing my attention.",
                    "signals": "Steady, absorbing, unobtrusive. Supports concentration without repeatedly grabbing my attention.",
                    "weights": {"instrumental": 2, "relaxed": 1, "intensity": -1,
                                "aggression": -2, "density": -1}},
    "cruise": {"label": "Cruise", "icon": "moon", "color": "lilac",
               "attributes": "Easygoing, moderately lively, comfortable company",
               "description": "Keeps everyday listening enjoyable without pushing me toward a particular mood.",
               "signals": "Easygoing, moderately lively, comfortable company. Keeps everyday listening enjoyable without pushing me toward a particular mood.",
               "weights": {"intensity": 3, "relaxed": 1, "aggression": -2},
               "targets": {"intensity": .5}},
    "energize": {"label": "Energize", "icon": "pulse", "color": "orange",
                 "attributes": "Driving, animated, propulsive",
                 "description": "Gives me momentum and makes me feel more active.",
                 "signals": "Driving, animated, propulsive. Gives me momentum and makes me feel more active.",
                 "weights": {"intensity": 1}},
    "uplift": {"label": "Lift my mood", "icon": "sun", "color": "gold",
               "attributes": "Positive, buoyant, encouraging",
               "description": "Gives an emotional lift, whether gentle or energetic.",
               "signals": "Positive, buoyant, encouraging. Gives an emotional lift, whether gentle or energetic.",
               "weights": {"happiness": 3, "sadness": -1}},
    "release": {"label": "Let off steam", "icon": "spark", "color": "rose",
                "attributes": "Forceful, hard-hitting, cathartic",
                "description": "Delivers intensity and impact; can be slow and heavy.",
                "signals": "Forceful, hard-hitting, cathartic. Delivers intensity and impact; can be slow and heavy.",
                "weights": {"aggression": 4, "intensity": 1}},
}


def axis_fit(preset, name, value):
    """A measured midpoint can be desirable; an unknown value stays neutral."""
    if value is None:
        return .5
    target = PRESETS[preset].get('targets', {}).get(name)
    if target is not None:
        return 1 - abs(value - target) / max(target, 1 - target)
    return value if PRESETS[preset]['weights'][name] > 0 else 1 - value


def current(store, now):
    mix_tuning.expire(store, now)
    value = store.settings.get_setting(SETTING)
    return value if value in PRESETS else ""


def affinity(item, preset, profiles=None):
    """Consume the prepared measurement/prediction/fallback layer without inference."""
    profile = _field(item, 'musical') or (profiles or {}).get(_field(item, 'key'))
    resolved = (profile or {}).get('vibes', {}).get('fits', {}).get(preset, {}).get('score')
    if type(resolved) in (int, float) and math.isfinite(resolved) and 0 <= resolved <= 1:
        return resolved
    return heuristic_affinity(profile, preset)


def heuristic_affinity(profile, preset):
    """The original fixed-denominator fallback, also used during preparation."""
    total = evidence = 0.0
    for name, weight in PRESETS[preset]["weights"].items():
        value, support = musical_character.dimension({'musical': profile}, name)
        if value is not None:
            # Intensity already contains conservative shrinkage. A midpoint
            # target still needs support: uncertainty is not measured moderation.
            reliability = (1 if name == 'intensity' and name not in PRESETS[preset].get('targets', {})
                           else min(1, support / .6))
            evidence += abs(weight) * (axis_fit(preset, name, value) - .5) * reliability
        total += abs(weight)
    return 0.5 + evidence / total


def taste_score(item):
    """Fixed bounded scale for actual scores, independent of pool size or position.

    This monotonic link is an engineering policy, not empirical calibration.
    Missing scores remain unknown/neutral and never inherit their list position.
    """
    score = _field(item, 'score')
    if type(score) not in (int, float) or not math.isfinite(score):
        return .5
    return .5 + .5 * score / (1 + abs(score))


def order_key(item, preset, profiles=None):
    fit = affinity(item, preset, profiles)
    return (-round((fit - .5) / FIT_STEP), -taste_score(item), -fit)


class Ranker:
    def __init__(self, store, now):
        self.store = store
        self.preset = current(store, now)
        self.profiles, self.loaded = {}, set()

    def _ensure(self, items):
        keys = {_field(item, 'key') for item in items
                if _field(item, 'key') and not (isinstance(item, dict) and 'musical' in item)} - self.loaded
        self.profiles.update(musical_character.load(self.store, sorted(keys)))
        self.loaded.update(keys)

    def affinity(self, item):
        if not self.preset:
            return .5
        self._ensure([item])
        return affinity(item, self.preset, self.profiles)

    def __call__(self, items):
        if not self.preset:
            return items
        self._ensure(items)
        return sorted(items, key=lambda item: order_key(item, self.preset, self.profiles))


def ranker(store, now):
    """Capture metadata once for a whole menu; preserve taste order when sound evidence ties."""
    return Ranker(store, now)


def rank(store, now, items):
    return ranker(store, now)(items)
