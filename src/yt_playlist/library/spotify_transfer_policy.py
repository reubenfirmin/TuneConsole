"""Detached Spotify selection, preview actions and retained-report policy."""
import math

from yt_playlist.util.matching import identity_key

JOB_KEY = 'spotify_library_import_job'
ACTIVE_STATUSES = {'queued', 'running'}


def retained_report(job):
    if job.get('report_schema', 0) >= 5 and job.get('report'):
        report = job['report']
        return {**job, 'report': {**report, 'details': {**report['details'], 'unmatched_tracks': []}}}
    return job


def recovered(job, now):
    return {**job, 'status': 'error', 'finished_at': now,
            'error': 'TuneConsole stopped before the import finished. Upload the same export '
                     'again to resume recorded transfers.'}


def estimate(parsed):
    tracks = [track for playlist in parsed['playlists'] for track in playlist['tracks']]
    lookups = len({identity_key(track['title'], track['artist']) for track in tracks}) + len(parsed['albums'])
    minutes = max(5, math.ceil(lookups / 12.5))
    hours = math.ceil(minutes / 60)
    label = (f'about {int(math.ceil(minutes / 5) * 5)} minutes' if minutes < 60 else
             f"up to about {hours} hour{'s' if hours != 1 else ''}")
    return {'playlists_found': len(parsed['playlists']), 'track_entries': len(tracks),
            'albums_found': len(parsed['albums']), 'lookup_count': lookups, 'estimate_label': label}


def selection(parsed, indices, include_albums):
    indices = sorted({int(value) for value in indices})
    if any(index < 0 or index >= len(parsed['playlists']) for index in indices):
        raise ValueError('Invalid playlist selection')
    selected = {'playlists': [parsed['playlists'][i] for i in indices],
                'albums': parsed['albums'] if include_albums else []}
    if not selected['playlists'] and not selected['albums']:
        raise ValueError('Select at least one playlist or saved albums')
    return selected


def album_actions(prepared, actions):
    albums = []
    for source, (action, selected) in zip(prepared['albums'], actions, strict=True):
        resolution = source['resolution']
        if action not in ('save', 'playlist', 'skip'):
            raise ValueError('Invalid album action')
        match = resolution['match'] if selected is None else next(
            (c for c in resolution['candidates'] if c['browse_id'] == selected), None)
        if selected and match is None:
            raise ValueError('Choose an album from this preview')
        if action == 'playlist' and match is None:
            raise ValueError('Choose a release to create as a playlist')
        albums.append({**source, 'action': action, 'resolution': {**resolution, 'match': match}})
    return {**prepared, 'albums': albums}
