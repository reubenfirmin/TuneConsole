"""Local rating projection, independent of remote writes and inbox replay."""
from yt_playlist.rec import graduation


def record_confirmed(store, account_id, video_id, status, now):
    with store._lock:
        record_observations(store, account_id, {video_id: status}, source='api', now=now)


def reconcile_recordings(store, keys):
    with store._lock:
        graduation.apply_ratings(store, store.tracks.recording_ratings(keys))


def record_observations(store, account_id, statuses, *, source, now):
    with store._lock:
        store.tracks.record_item_ratings(account_id, statuses, source=source, now=now)
        retained = store.tracks.retained_rating_statuses(account_id, statuses,
            newer_than=now if source == 'sync' else None)
        for video_id, status in retained.items():
            store.playlists.set_song_liked(account_id, video_id, status == 'LIKE')
        reconcile_recordings(store, {store.tracks.identity_key_for_video(video) for video in retained})
