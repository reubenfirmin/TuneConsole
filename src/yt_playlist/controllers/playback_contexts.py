"""Exact playback attribution, with catalog observations committed first."""
from yt_playlist.core.playback_context_policy import pending_album

BINDING_LIMIT = 64


class PlaybackContexts:
    def __init__(self, repository, tracks, collection, guard):
        self.repository, self.tracks, self.guard = repository, tracks, guard
        self.collection = collection

    def album(self):
        return self.repository.album_context_values()

    def remember_album(self, value):
        with self.guard():
            value = pending_album(value)
            album_id = self.collection.ensure_album_identity(value['browse']) if value is not None else None
            for video in dict.fromkeys(value.get('videos', []) if value is not None else ()):
                if not self.repository.video_exists(video):
                    self.tracks.observe_track_entry(video, '', '', preferred_key='@unresolved-video:' + video)
            return self.repository.put_album_context(value, album_id)

    def bind_album(self, pending, account_id):
        with self.guard():
            return self.repository.bind_album_context(pending, account_id)

    def bind_choice(self, account_id, playlist, video, opportunity, option, now, window_s, *, response_id=None):
        with self.guard():
            if not self.repository.video_exists(video):
                self.tracks.observe_track_entry(video, '', '', preferred_key='@unresolved-video:' + video)
            return self.repository.put_choice_binding(account_id, playlist, video, opportunity, str(option), now,
                                                      before=now - window_s, limit=BINDING_LIMIT, response_id=response_id)

    def choice(self, account_id, playlist, video, now, window_s):
        if playlist is None and not video:
            return None
        return self.repository.choice_binding(account_id, playlist, video, before=now - window_s, through=now)
