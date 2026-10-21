"""Temporary, explicit sound preferences for Home mixes, applied before track selection."""
import math

from yt_playlist.rec import mix_tuning
from yt_playlist.rec.ordering import _field
from yt_playlist.util import genre_map

SETTING = "home_mix_vibe"

# Desired direction and relative importance. Unknown features contribute neutral evidence;
# instrumentalness or happiness must never be inferred from genre.
PRESETS = {
    "gentle": {"label": "Keep it gentle", "icon": "feather", "color": "mint",
               "description": "Less intensity. A little breathing room.",
               "signals": "Lower energy · slower tempo · relaxed mood",
               "weights": {"energy": -3, "bpm": -1, "mood_relaxed": 2, "loudness": -1}},
    "boost": {"label": "Pick me up", "icon": "sun", "color": "gold",
              "description": "For the afternoon dip. Find a second wind.",
              "signals": "More energy · faster tempo · brighter mood",
              "weights": {"energy": 3, "bpm": 2, "mood_happy": 1}},
    "focus": {"label": "Headspace", "icon": "orbit", "color": "blue",
              "description": "Settle in. Give your thoughts some space.",
              "signals": "More instrumental · relaxed mood · lower energy",
              "weights": {"instrumental": 4, "mood_relaxed": 2, "energy": -1}},
    "bright": {"label": "Sunny side up", "icon": "spark", "color": "rose",
               "description": "A brighter soundtrack to whatever’s next.",
               "signals": "Happier mood · less melancholy · a little groove",
               "weights": {"mood_happy": 3, "mood_sad": -2, "danceability": 1, "energy": 1}},
    "unwind": {"label": "After hours", "icon": "moon", "color": "lilac",
               "description": "Day done. Turn the intensity down.",
               "signals": "Acoustic character · relaxed mood · slower tempo",
               "weights": {"mood_acoustic": 2, "mood_relaxed": 2, "bpm": -1, "energy": -2}},
    "move": {"label": "Let’s move", "icon": "pulse", "color": "orange",
             "description": "Kitchen dancing absolutely counts.",
             "signals": "More danceable · higher energy · faster tempo",
             "weights": {"danceability": 4, "energy": 2, "bpm": 1}},
}


def current(store, now):
    mix_tuning.expire(store, now)
    value = store.settings.get_setting(SETTING)
    return value if value in PRESETS else ""


def audio_features(store):
    # Discoveries carry the same features as owned tracks. Prefer library metadata on overlap.
    audio = {t["identity_key"]: t["audio"] for t in store.discovery.get_discovered_tracks()}
    for key, features in store.rec_query.track_audio_features().items():
        audio[key] = {**audio.get(key, {}), **features}
    return audio


def affinity(item, preset, audio):
    """0..1 sound match, with conservative genre energy as the only metadata fallback."""
    features = audio.get(_field(item, "key"), {})
    total = evidence = 0.0
    for name, weight in PRESETS[preset]["weights"].items():
        value = features.get(name)
        confidence = 1.0
        if value is None and name == "energy" and _field(item, "genre"):
            value = genre_map.energy(_field(item, "genre"))
            confidence = 0.5
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        if value is not None and math.isfinite(value):
            if name == "bpm":
                value = (value - 60) / 120
            value = max(0.0, min(1.0, value))
            evidence += weight * (value - 0.5) * confidence
        total += abs(weight)
    return 0.5 + evidence / total


class Ranker:
    def __init__(self, store, now):
        self.preset = current(store, now)
        self.audio = audio_features(store) if self.preset else {}

    def affinity(self, item):
        return affinity(item, self.preset, self.audio) if self.preset else 0.5

    def __call__(self, items):
        if not self.preset:
            return items
        # Sound leads; a small original-rank contribution keeps personal taste in the mix.
        size = max(1, len(items) - 1)
        return [item for _, item in sorted(enumerate(items),
                key=lambda pair: -(self.affinity(pair[1]) - 0.12 * pair[0] / size))]


def ranker(store, now):
    """Capture metadata once for a whole menu; preserve taste order when sound evidence ties."""
    return Ranker(store, now)


def rank(store, now, items):
    return ranker(store, now)(items)
