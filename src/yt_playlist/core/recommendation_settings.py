"""Neutral saved-setting definitions shared by queries and recommendation policy."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntegerSetting:
    name: str
    minimum: int
    maximum: int
    default: int

    def parse(self, raw):
        if raw is None or raw == '':
            return self.default
        try:
            return int(round(max(self.minimum, min(self.maximum, float(raw)))))
        except (TypeError, ValueError):
            return self.default


MAX_TRACK_MINUTES = IntegerSetting('max_track_minutes', 1, 180, 20)
LANES = [
    ('neighbourhood', 'Taste matches', 'Tracks matched to your playlists, listening history, and preferences.'),
    ('deep_cut', 'Deep cuts', 'Overlooked tracks by artists you play often.'),
    ('rotation', 'Playlist neighbors', 'Tracks found in the same playlists as your favorites.'),
]
LANE_NAMES = frozenset(name for name, _label, _help in LANES)
