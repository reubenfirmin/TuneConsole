"""Genre endpoints for the radio dial, independent of embedding style and I/O."""
from functools import lru_cache

from yt_playlist.util import genre_catalog, genre_map


@lru_cache(maxsize=4096)
def _key(label):
    normalized = genre_catalog.normalize(label)
    return genre_catalog.catalog()[1].get(normalized, (normalized,))[0]


def genre(track):
    return _key((track or {}).get("genre") or "")


class GenrePolicy:
    def __init__(self, theme, history, *, current=None):
        self.current = current if current is not None else history[-1] if history else None
        self.pinned = theme == 1
        self.breadth = max(0., 1 - 2 * theme)
        self.focus = max(0., 2 * theme - 1)
        self.recent = [(genre(t), genre_map.family(genre(t)), .8 ** age)
                       for age, t in enumerate(reversed(history[-8:])) if genre(t)]
        self.weight = sum(weight for _, _, weight in self.recent)
        self._contrasts = {}

    def allows(self, track):
        # An unknown genre cannot establish or satisfy a genre pin.
        if not self.pinned:
            return True
        current = genre(self.current)
        return bool(current) and genre(track) == current

    def contrast(self, track):
        """Prefer a genre change, then less recent repetition, then broader contrast.

        Unknown tags never count as evidence of a different genre. Family distance
        breaks ties between actual genre changes; it does not replace genre identity.
        """
        key = genre(track)
        if not self.recent:
            return (0., 0., 0.)
        if not key:
            return (-1., 0., 0.)
        if key not in self._contrasts:
            family = genre_map.family(key)
            absent = 1 - sum(w for previous, _, w in self.recent if previous == key) / self.weight
            distance = sum(w * genre_map.family_distance(family, previous)
                           for _, previous, w in self.recent) / self.weight
            self._contrasts[key] = (float(key != self.recent[0][0]), absent, distance)
        return self._contrasts[key]

    def bonus(self, track):
        changed, absent, distance = self.contrast(track)
        contrast = (max(0., changed) + absent + distance) / 3
        return self.breadth * contrast + self.focus * bool(genre(self.current)) * (
            genre(track) == genre(self.current))
