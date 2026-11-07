"""Collection coverage rules and page shapes built from persisted facts."""

from .collection_policy import RETURN_DAYS


def album_share(inputs):
    if inputs['layout'] == 'missing':
        return None
    if inputs['layout'] == 'frozen':
        videos = inputs['videos']
    else:
        rows = inputs['items']
        if not rows or any(row['position'] != index or not row['video_id'] for index, row in enumerate(rows)):
            return None
        videos = [row['video_id'] for row in rows]
    return {'title': inputs['title'], 'video_ids': videos}


def taste_album_contexts(inputs, include_browse=()):
    albums = {}
    for row in inputs['tracks']:
        album = albums.setdefault(row['id'], {key: row[key] for key in
            ('id', 'title', 'artist', 'browse', 'year', 'genre', 'thumbnail', 'saved',
             'complete', 'expected_track_count')})
        album.setdefault('tracks', {})[row['occurrence']] = row['key'], row['is_full']
    result, included = [], set(include_browse)
    for album in albums.values():
        tracks = album.pop('tracks').values()
        full = [key for key, is_full in tracks if is_full]
        fetched, expected = album.pop('complete'), album.pop('expected_track_count')
        album['tracklist_complete'] = bool(fetched and full and all(full) and expected == len(full))
        album['keys'] = sorted(set(full if album['tracklist_complete'] else (key for key, _ in tracks if key)))
        album['track_days'] = {key: inputs['days'].get(key, 0) for key in album['keys']}
        album['return_days'] = max(album['track_days'].values(), default=0)
        album['return_coverage'] = (sum(days >= RETURN_DAYS for days in album['track_days'].values())
            / len(album['keys']) if album['tracklist_complete'] else None)
        if album['saved'] or album['return_days'] >= RETURN_DAYS or album['browse'] in included:
            result.append(album)
    return sorted(result, key=lambda album: album['id'])


def playback_source(inputs, playlist_ytm_id, album_browse_id):
    playlist = inputs['playlist']
    if playlist_ytm_id:
        if playlist:
            return {'kind': 'playlist', 'id': playlist['id'], 'title': playlist['title'] or 'Playlist'}
        if not playlist_ytm_id.startswith('OLAK5uy_'):
            return None
    albums = inputs['albums']
    if album_browse_id:
        return {'kind': 'album', 'browse': album_browse_id, 'title': (albums[0]['title'] if albums else None) or 'Album'}
    if len(albums) == 1:
        return {'kind': 'album', 'browse': albums[0]['browse'], 'title': albums[0]['title'] or 'Album'}
    return None


def collection_albums(inputs):
    albums = {}
    for row in inputs['tracks']:
        album = albums.setdefault(row['album_id'], {
            'album': row['album'], 'artist': row['artist'], 'browse': row['browse'],
            'thumb': None, 'songs': 0, 'plays': 0, '_pls': set()})
        album['songs'] += 1
        album['plays'] += inputs['plays'].get(row['key'], 0)
        album['thumb'] = album['thumb'] or row['thumb']
        album['_pls'].update(filter(None, (row['pls'] or '').split(',')))
    for album in albums.values():
        album['n_pls'] = len(album.pop('_pls'))
    return sorted(albums.values(), key=lambda album: (-album['plays'], album['album'].lower()))


def collection_artists(inputs):
    artists = {}

    def artist_row(row):
        return artists.setdefault(row['artist_id'] or ('unidentified', row['artist']), {
            'artist': row['artist'], 'artist_id': row['artist_id'], 'browse': row['browse'],
            'plays': 0, 'thumbnail': None, '_songs': set(), '_albums': set(), '_pls': set()})

    for row in inputs['tracks']:
        artist = artist_row(row)
        if row['key'] not in artist['_songs']:
            artist['plays'] += inputs['plays'].get(row['key'], 0)
        artist['_songs'].add(row['key'])
        if row['album_browse']:
            artist['_albums'].add(row['album_id'])
        artist['thumbnail'] = artist['thumbnail'] or row['thumb']
        artist['_pls'].update(filter(None, (row['pls'] or '').split(',')))
    for row in inputs['saved']:
        artist = artist_row(row)
        artist['_albums'].add(row['album_id'])
        artist['thumbnail'] = artist['thumbnail'] or row['thumbnail']
    for artist in artists.values():
        artist['songs'] = len(artist.pop('_songs'))
        artist['n_albums'] = len(artist.pop('_albums'))
        artist['n_pls'] = len(artist.pop('_pls'))
    return sorted(artists.values(), key=lambda artist: (-artist['plays'], artist['artist'].lower()))


def artist_albums(inputs):
    albums = {row['id']: {**row, 'songs': 0, 'plays': 0, '_pls': set()} for row in inputs['albums']}
    if inputs['singles']:
        albums[None] = dict(id=None, album='Singles / no album', year=None, thumb=None,
            browse=None, saved=False, songs=0, plays=0, _pls=set())
    for row in inputs['tracks']:
        album = albums.get(row['album_id'])
        if album is None:
            continue
        album['songs'] += 1
        album['plays'] += inputs['plays'].get(row['identity_key'], 0)
        album['thumb'] = album['thumb'] or row['thumb']
        album['_pls'].update(filter(None, (row['pls'] or '').split(',')))
    for album in albums.values():
        album['n_pls'] = len(album.pop('_pls'))
        album['saved'] = bool(album['saved'])
    return sorted(albums.values(), key=lambda album: (-album['plays'], (album['album'] or '').casefold(), album['id'] or 0))
