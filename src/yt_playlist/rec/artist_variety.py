"""Detached performing-artist variety rules shared by playlist selectors."""
from collections import Counter
from collections.abc import Mapping

VERSION = 1


def members(item):
    """Unknown credits stay opaque; punctuation never establishes artist identity."""
    get = item.get if isinstance(item, Mapping) else lambda key, default=None: getattr(item, key, default)
    identities = get('artist_ids')
    if identities:
        return frozenset(identities)
    label = (get('artist') or '').strip().casefold()
    return frozenset(['credit:' + label]) if label else frozenset()


class Selection:
    """Prefer caps, retain a bounded ranked reserve, relax only to fill a mix."""
    def __init__(self, artist_cap, album_cap, *, limit=None, minimum=12, initial=()):
        self.artist_cap, self.album_cap = artist_cap, album_cap
        self.limit, self.minimum = limit, min(minimum, limit) if limit is not None else minimum
        self.selected, self.reserve = list(initial), []
        self.artists, self.albums = Counter(), Counter()
        self.seen = {row['key'] for row in initial if row.get('key')}
        for row in initial:
            self._count(row)

    def _count(self, row):
        self.artists.update(members(row))
        if row.get('album'):
            self.albums[row['album']] += 1

    def add(self, row):
        if self.limit is not None and len(self.selected) >= self.limit:
            return True
        key = row.get('key')
        if key and key in self.seen:
            return False
        album = row.get('album')
        if (any(self.artists[artist] >= self.artist_cap for artist in members(row))
                or album and self.albums[album] >= self.album_cap):
            if len(self.reserve) < self.minimum:
                self.reserve.append(row)
                if key:
                    self.seen.add(key)
        else:
            self.selected.append(row)
            self._count(row)
            if key:
                self.seen.add(key)
        return self.limit is not None and len(self.selected) >= self.limit

    def finish(self):
        self.selected.extend(self.reserve[:max(0, self.minimum - len(self.selected))])
        return self.selected


def select(rows, artist_cap, album_cap, *, limit=None, minimum=12, initial=()):
    selection = Selection(artist_cap, album_cap, limit=limit, minimum=minimum, initial=initial)
    for row in rows:
        if selection.add(row):
            break
    return selection.finish()


