"""Track query controller: local facts with a live-fallback acquisition for unknown videos."""
from datetime import datetime, timezone

from yt_playlist.core.accounts import catalog_client
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.thumbnails import best_thumb


def detail(store, client_provider, video_id, hints, logger=None):
    """Return (track, known). `hints` are query-string defaults for an unknown track."""
    track = store.tracks.track_detail(video_id)
    known = track is not None
    if track is None:
        track = {field: (hints.get(field) or '')[:1000] for field in
                 ('title', 'artist', 'album', 'album_browse', 'thumbnail', 'duration')}
        # Most discovery links already carry their preview. A bare shared URL can
        # still resolve live without acquiring library or discovery membership.
        if not track['title']:
            try:
                client = catalog_client(client_provider())
                details = (client.get_song(video_id) if client else {}).get('videoDetails') or {}
                track.update(title=details.get('title') or '', artist=details.get('author') or '',
                             duration=details.get('lengthSeconds'),
                             thumbnail=best_thumb((details.get('thumbnail') or {}).get('thumbnails')))
            except Exception:
                if logger is not None:
                    logger.info('Track preview unavailable for %s', video_id, exc_info=True)
        track['video_id'] = video_id
        track['playlists'] = []
    track['duration'] = parse_duration(track.get('duration'))
    for field in ('artist', 'album'):
        track[field] = track.get(field) or ''
    track['title'] = track.get('title') or 'Unknown track'
    track['thumbnail'] = track.get('thumbnail') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
    return track, known


def history(store, video_id, page, track=None):
    ledger, more = store.tracks.track_play_ledger(track['recording_id'], page=page) if track else ([], False)
    for row in ledger:
        at = datetime.fromtimestamp(row['sort_at'], timezone.utc)
        row['at_iso'] = at.isoformat()
        row['at_label'] = at.strftime('%Y-%m-%d %H:%M UTC')
    return {'track': track or {'video_id': video_id}, 'ledger': ledger, 'more': more, 'page': page}


def profile_evidence(store, track, known):
    if not known:
        return None, [], False
    profile = store.musical_profiles.by_keys([track['identity_key']]).get(track['identity_key'])
    if not profile:
        return None, [], False
    sources, truncated = store.musical_profiles.provenance_sources(
        track['identity_key'], profile.get('source_versions', []))
    return profile, sources, truncated
