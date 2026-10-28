"""Checked, release-specific artwork, independent of recommendation ranking.

Artwork provenance lives in a reviewable JSON ledger. The worker copies matched
photography into its immutable evidence artifacts; SQLite holds the display
projection. Copy acquisition is entirely mechanical and does not use this ledger.
"""
import json
from functools import lru_cache
from pathlib import Path

from yt_playlist.providers.discogs import _artist_key
from yt_playlist.util.matching import recording_title


@lru_cache(maxsize=1)
def _entries():
    return json.loads((Path(__file__).parent.parent / 'data' / 'album_editorial.json').read_text())['entries']


def lookup(album):
    if not album.get('artist') or not album.get('title'):
        return None
    for entry in _entries():
        artists = [entry['artist'], *entry.get('artist_aliases', [])]
        titles = [entry['title'], *entry.get('title_aliases', [])]
        if (_artist_key(album['artist']) in {_artist_key(artist) for artist in artists}
                and recording_title(album['title']) in {recording_title(title) for title in titles}):
            # A defensive copy: presentation code cannot mutate checked evidence.
            return json.loads(json.dumps(entry))
    return None
