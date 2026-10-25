"""Temporary, explicit sound preferences for Home mixes, applied before track selection."""
import math

from yt_playlist.rec import mix_tuning, musical_character
from yt_playlist.rec.ordering import _field

SETTING = "home_mix_vibe"
VERSION = 'musical-vibes-2'
FIT_STEP = .04

# Desired direction and relative importance. Unknown features contribute neutral evidence;
# instrumentalness or happiness must never be inferred from genre.
PRESETS = {
    "gentle": {"label": "Keep it gentle", "icon": "feather", "color": "mint",
               "description": "Less intensity. A little breathing room.",
               "signals": "Lower energy · slower tempo · relaxed mood",
               "weights": {"intensity": -3, "tempo": -1, "relaxed": 2, "density": -1}},
    "boost": {"label": "Pick me up", "icon": "sun", "color": "gold",
              "description": "For the afternoon dip. Find a second wind.",
              "signals": "More energy · faster tempo · brighter mood",
              "weights": {"intensity": 3, "tempo": 2, "happiness": 1}},
    "focus": {"label": "Headspace", "icon": "orbit", "color": "blue",
              "description": "Settle in. Give your thoughts some space.",
              "signals": "More instrumental · relaxed mood · lower energy",
              "weights": {"instrumental": 3, "relaxed": 2, "intensity": -1, "density": -2,
                          "aggression": -1}},
    "bright": {"label": "Sunny side up", "icon": "spark", "color": "rose",
               "description": "A brighter soundtrack to whatever’s next.",
               "signals": "Happier mood · less melancholy · a little groove",
               "weights": {"happiness": 3, "sadness": -2, "groove": 1, "intensity": 1}},
    "unwind": {"label": "After hours", "icon": "moon", "color": "lilac",
               "description": "Day done. Turn the intensity down.",
               "signals": "Acoustic character · relaxed mood · slower tempo",
               "weights": {"acoustic": 2, "relaxed": 2, "tempo": -1, "intensity": -2}},
    "move": {"label": "Let’s move", "icon": "pulse", "color": "orange",
             "description": "Kitchen dancing absolutely counts.",
             "signals": "More danceable · higher energy · faster tempo",
             "weights": {"groove": 4, "intensity": 2, "tempo": 1}},
}


def current(store, now):
    mix_tuning.expire(store, now)
    value = store.settings.get_setting(SETTING)
    return value if value in PRESETS else ""


def affinity(item, preset, profiles=None):
    """Fixed-denominator fit over one saved profile; missing axes stay neutral."""
    profile = _field(item, 'musical') or (profiles or {}).get(_field(item, 'key'))
    total = evidence = 0.0
    for name, weight in PRESETS[preset]["weights"].items():
        value, support = musical_character.dimension({'musical': profile}, name)
        if value is not None:
            # Intensity already contains the resolver's conservative shrinkage.
            reliability = 1 if name == 'intensity' else min(1, support / .6)
            evidence += weight * (value - .5) * reliability
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
