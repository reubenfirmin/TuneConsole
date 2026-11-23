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


def _edit_key(track):
    title = track.get('orig_title')
    artist = track.get('artist_browse_id')
    duration = track.get('duration')
    if title and artist and duration and duration > 0:
        return title, artist, duration
    return None


def distinct_edits(tracks):
    """Keep the first upload with the same full title, YouTube artist ID and length."""
    recordings = set()
    seen = set()
    for track in tracks:
        key = track.get('key')
        if key and key in recordings:
            continue
        edit = _edit_key(track)
        if edit is not None:
            if edit in seen:
                continue
            seen.add(edit)
        if key:
            recordings.add(key)
        yield track


class Selection:
    """Prefer caps, retain a bounded ranked reserve, relax only to fill a mix."""
    def __init__(self, artist_cap, album_cap, *, limit=None, minimum=12, initial=(), unique_edits=False):
        self.artist_cap, self.album_cap = artist_cap, album_cap
        self.limit, self.minimum = limit, min(minimum, limit) if limit is not None else minimum
        self.selected, self.reserve = list(initial), []
        self.artists, self.albums = Counter(), Counter()
        self.seen = {row['key'] for row in self.selected if row.get('key')}
        self.edits = set() if unique_edits else None
        self.reserve_edits = set()
        for row in self.selected:
            self._count(row)
            if self.edits is not None and (edit := _edit_key(row)) is not None:
                self.edits.add(edit)

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
        edit = _edit_key(row) if self.edits is not None else None
        if edit is not None and edit in self.edits:
            return False
        album = row.get('album')
        if (any(self.artists[artist] >= self.artist_cap for artist in members(row))
                or album and self.albums[album] >= self.album_cap):
            if len(self.reserve) < self.minimum and edit not in self.reserve_edits:
                self.reserve.append((row, edit))
                if edit is not None:
                    self.reserve_edits.add(edit)
                if key:
                    self.seen.add(key)
        else:
            self.selected.append(row)
            self._count(row)
            if edit is not None:
                self.edits.add(edit)
            if key:
                self.seen.add(key)
        return self.limit is not None and len(self.selected) >= self.limit

    def finish(self):
        for row, edit in self.reserve:
            if len(self.selected) >= self.minimum:
                break
            if edit is None or edit not in self.edits:
                self.selected.append(row)
        return self.selected


def select(rows, artist_cap, album_cap, *, limit=None, minimum=12, initial=(), unique_edits=False):
    selection = Selection(artist_cap, album_cap, limit=limit, minimum=minimum, initial=initial,
                          unique_edits=unique_edits)
    for row in rows:
        if selection.add(row):
            break
    return selection.finish()


