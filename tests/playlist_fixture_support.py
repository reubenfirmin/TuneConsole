"""Complete remote readback for fixtures describing successful playlist writes."""
from tests.conftest import _track


def remote_catalog(tracks):
    return {track['video_id']: _track(track['video_id'], track['title'], track['artist'],
            track.get('duration'), track.get('album', '')) for track in tracks if track.get('video_id')}


def library_catalog(store):
    return remote_catalog([dict(row) for row in store.conn.execute(
        'SELECT video_id,title,artist,album,duration_s AS duration FROM catalog_tracks '
        'WHERE video_id IS NOT NULL')])
