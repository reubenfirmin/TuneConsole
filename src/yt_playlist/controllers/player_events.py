"""Listening interpretations composed from raw persisted player events."""
import json

from yt_playlist.library.listen_derive import classify_exit, skip_blackouts, SKIP_HISTORY_DAYS


class PlayerEvents:
    def __init__(self, repository):
        self.repository = repository

    def record_player_event(self, account_id, kind, video_id, position, duration,
                            playlist_ytm_id, payload, at, *, capture_id=None):
        if capture_id is not None:
            payload = json.dumps({**json.loads(payload or '{}'), 'capture_id': capture_id})
            return self.repository.record_player_event(
                account_id, kind, video_id, position, duration, playlist_ytm_id, payload, at,
                capture_id=capture_id)
        return self.repository.record_player_event(
            account_id, kind, video_id, position, duration, playlist_ytm_id, payload, at)

    def player_events_since(self, since_ts, kind=None):
        return self.repository.player_events_since(since_ts, kind)

    def event_for_capture(self, account_id, at, capture_id):
        return self.repository.event_for_capture(account_id, at, capture_id)

    def playback_activity(self, since, until):
        return self.repository.playback_activity(since, until)

    def latest_id(self):
        return self.repository.latest_id()

    def activity_added_before(self, after_id, through):
        return self.repository.activity_added_before(after_id, through)

    def listening_exits(self, since_ts):
        return self.repository.listening_exits(since_ts)

    def rating_advance_exit_ids(self, event_ids):
        return self.repository.rating_advance_exit_ids(event_ids)

    def recent_skips_with_ts(self, since_ts):
        latest = {}
        for row in self.repository.library_exits(since_ts):
            if row['key'] is not None and classify_exit(row['position'], row['duration']) == 'skip':
                latest.setdefault(row['key'], float(row['at']))
        return sorted(latest.items(), key=lambda item: item[1], reverse=True)

    def skip_blackouts(self, now, *, keys=None):
        keys = list(dict.fromkeys(keys)) if keys is not None else None
        if keys == []:
            return {}
        rows = self.repository.recording_exits(now - SKIP_HISTORY_DAYS * 86400, now, keys=keys)
        return skip_blackouts(rows, now)
