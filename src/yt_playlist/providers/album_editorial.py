"""Checked, release-specific artwork, independent of recommendation ranking.

Artwork provenance lives in a reviewable JSON ledger. The worker copies matched
photography into its immutable evidence artifacts; SQLite holds the display
projection. Copy acquisition is entirely mechanical and does not use this ledger.
"""
import json
from yt_playlist.util.matching import artist_key, recording_title


def lookup(album, entries):
    if not album.get('artist') or not album.get('title'):
        return None
    for entry in entries:
        artists = [entry['artist'], *entry.get('artist_aliases', [])]
        titles = [entry['title'], *entry.get('title_aliases', [])]
        if (artist_key(album['artist']) in {artist_key(artist) for artist in artists}
                and recording_title(album['title']) in {recording_title(title) for title in titles}):
            # A defensive copy: presentation code cannot mutate checked evidence.
            return json.loads(json.dumps(entry))
    return None
