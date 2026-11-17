"""Omnisearch selection and presentation over repository results."""
import random
from urllib.parse import quote
from yt_playlist.util.navigation import album_url, artist_url, playlist_url, track_url
from yt_playlist.util.music_matching import relevance
from yt_playlist.util.music_catalog import section_order

MIN_QUERY = 2
CANDIDATE_CAP = 50
GENERATED_GROUP = 'Generated'


def _sample(rows, query, count):
    rows = list(rows)
    return rows if len(rows) <= count else random.Random(query.lower()).sample(rows, count)


def _tracks(rows):
    return [{'type': 'track', 'label': row['title'], 'sub': row['artist'],
             'href': track_url(row['vid']),
             'video_id': row['vid'], 'song': {'video_id': row['vid'], 'title': row['title'],
                 'artist': row['artist'], 'thumbnail': row['thumb']},
             'thumbnail': row['thumb'], 'external': False} for row in rows]


def _playlists(rows):
    return [{'type': 'playlist', 'label': row['title'], 'sub': 'playlist',
             'href': playlist_url(row['pid']), 'thumbnail': row['thumb'],
             'play_url': 'https://music.youtube.com/playlist?list=' + quote(row['ytm'], safe=''),
             'external': False} for row in rows]


def _artist_play(row):
    if row.get('browse_id'):
        return 'https://music.youtube.com/browse/' + quote(row['browse_id'], safe='')
    if row.get('video_id'):
        return 'https://music.youtube.com/watch?v=' + quote(row['video_id'], safe='')
    if row.get('album_browse'):
        return 'https://music.youtube.com/browse/' + quote(row['album_browse'], safe='')
    return None


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
        artist_tracks, shown_pids = [], set()
        if artists:
            sections.append({'kind': 'artist', 'title': 'Artists', 'rows': [
                {'type': 'artist', 'label': row['artist'], 'artist_id': row['artist_id'],
                 'sub': f"{row['n']} track" + ('' if row['n'] == 1 else 's'),
                 'href': artist_url(row['artist_id']),
                 'play_url': _artist_play(row),
                 'thumbnail': row['thumb'], 'external': False} for row in artists]})
            first = artists[0]
            primary = {'artist_id': first['artist_id'], 'name': first['artist'],
                       'thumbnail': first['thumb']}
            artist_id, name = primary['artist_id'], primary['name']
            artist_tracks = repository.artist_tracks(artist_id, limit=CANDIDATE_CAP)
            albums = _sample(repository.artist_albums(artist_id, limit=CANDIDATE_CAP), query, per_section)
            if albums:
                sections.append({'kind': 'albums_by', 'title': f'Albums by {name}', 'rows': [
                    {'type': 'album', 'label': row['album'], 'sub': name, 'browse_id': row['browse'],
                     'href': album_url(row['browse']),
                     'play_url': 'https://music.youtube.com/browse/' + quote(row['browse'], safe=''),
                     'thumbnail': row['thumb'], 'external': False} for row in albums]})
            playlists = _sample(repository.artist_playlists(
                artist_id, excluded_group=GENERATED_GROUP, limit=CANDIDATE_CAP), query, per_section)
            if playlists:
                shown_pids = {row['pid'] for row in playlists}
                sections.append({'kind': 'playlists_featuring', 'title': f'Playlists featuring {name}',
                                 'rows': _playlists(playlists)})
        # Artist aliases can match without appearing in the display credit.
        # Merge those artist results with direct matches before ranking/capping,
        # so every recording appears in one Songs section.
        candidates = {row['key']: row for row in artist_tracks}
        candidates.update((row['key'], row) for row in repository.song_matches(query, limit=CANDIDATE_CAP))
        songs = sorted(candidates.values(),
                       key=lambda row: relevance(query, row['title'], row['artist']), reverse=True)[:per_section]
        if songs:
            sections.insert(1 if artists else 0, {'kind': 'songs', 'title': 'Songs', 'rows': _tracks(songs)})
        playlists = _sample([row for row in repository.playlist_matches(
            query, excluded_group=GENERATED_GROUP, limit=CANDIDATE_CAP)
            if row['pid'] not in shown_pids], query, per_section)
        if playlists:
            sections.append({'kind': 'playlists', 'title': 'Playlists', 'rows': _playlists(playlists)})
        sections.sort(key=lambda section: section_order(section['rows'][0]['type']))
        return {'query': query, 'primary_artist': primary, 'sections': sections}
