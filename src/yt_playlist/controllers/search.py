"""Omnisearch selection and presentation over repository results."""
import random
from urllib.parse import quote

MIN_QUERY = 2
CANDIDATE_CAP = 50
GENERATED_GROUP = 'Generated'


def _sample(rows, query, count):
    rows = list(rows)
    return rows if len(rows) <= count else random.Random(query.lower()).sample(rows, count)


def _tracks(rows):
    return [{'type': 'track', 'label': row['title'], 'sub': row['artist'],
             'href': '/track/' + quote(row['vid'], safe=''),
             'thumbnail': row['thumb'], 'external': False} for row in rows]


def _playlists(rows):
    return [{'type': 'playlist', 'label': row['title'], 'sub': 'playlist',
             'href': f"/playlist/{row['pid']}", 'thumbnail': row['thumb'],
             'external': False} for row in rows]


class Search:
    def __init__(self, repository):
        self.repository = repository

    def omni_search(self, query, *, per_section=5):
        query = (query or '').strip()
        if len(query) < MIN_QUERY:
            return {'query': query, 'primary_artist': None, 'sections': []}
        repository = self.repository
        sections, primary = [], None
        artists = repository.artist_matches(query, limit=3)
        shown_keys, shown_pids = set(), set()
        if artists:
            sections.append({'kind': 'artist', 'title': 'Artist', 'rows': [
                {'type': 'artist', 'label': row['artist'], 'artist_id': row['artist_id'],
                 'sub': f"{row['n']} track" + ('' if row['n'] == 1 else 's'),
                 'href': f"/artist?artist_id={row['artist_id']}",
                 'thumbnail': row['thumb'], 'external': False} for row in artists]})
            first = artists[0]
            primary = {'artist_id': first['artist_id'], 'name': first['artist'],
                       'thumbnail': first['thumb']}
            artist_id, name = primary['artist_id'], primary['name']
            tracks = _sample(repository.artist_tracks(artist_id, limit=CANDIDATE_CAP), query, per_section)
            if tracks:
                shown_keys = {row['key'] for row in tracks}
                sections.append({'kind': 'tracks_by', 'title': f'Tracks by {name}', 'rows': _tracks(tracks)})
            albums = _sample(repository.artist_albums(artist_id, limit=CANDIDATE_CAP), query, per_section)
            if albums:
                sections.append({'kind': 'albums_by', 'title': f'Albums by {name}', 'rows': [
                    {'type': 'album', 'label': row['album'], 'sub': name,
                     'href': '/album?browse=' + quote(row['browse']),
                     'thumbnail': row['thumb'], 'external': False} for row in albums]})
            playlists = _sample(repository.artist_playlists(
                artist_id, excluded_group=GENERATED_GROUP, limit=CANDIDATE_CAP), query, per_section)
            if playlists:
                shown_pids = {row['pid'] for row in playlists}
                sections.append({'kind': 'playlists_featuring', 'title': f'Playlists featuring {name}',
                                 'rows': _playlists(playlists)})
        songs = _sample([row for row in repository.song_matches(query, limit=CANDIDATE_CAP)
                         if row['key'] not in shown_keys], query, per_section)
        if songs:
            sections.append({'kind': 'songs', 'title': 'Songs', 'rows': _tracks(songs)})
        playlists = _sample([row for row in repository.playlist_matches(
            query, excluded_group=GENERATED_GROUP, limit=CANDIDATE_CAP)
            if row['pid'] not in shown_pids], query, per_section)
        if playlists:
            sections.append({'kind': 'playlists', 'title': 'Playlists', 'rows': _playlists(playlists)})
        return {'query': query, 'primary_artist': primary, 'sections': sections}
