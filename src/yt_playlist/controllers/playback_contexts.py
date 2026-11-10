"""Exact playback attribution, with catalog observations committed first."""
import json

from yt_playlist.core import entity_names
from yt_playlist.core.playback_context_policy import pending_album

BINDINGS_KEY = 'listening_playback_bindings'
BINDING_LIMIT = 64
ALBUM_KEY = 'listening_album_pending'


class PlaybackContexts:
    def __init__(self, repository, settings, tracks, collection, guard):
        self.repository, self.settings, self.tracks, self.guard = repository, settings, tracks, guard
        self.collection = collection

    def album(self):
        if self.repository.album_context_available():
            return self.repository.album_context_values()
        return entity_names.account_document(json.loads(self.settings.get_setting(ALBUM_KEY) or 'null'))

    def remember_album(self, value):
        with self.guard():
            if not self.repository.album_context_available():
                return self.settings.set_setting(ALBUM_KEY, json.dumps(entity_names.external_account_document(
                    entity_names.account_document(value))))
            value = pending_album(value)
            album_id = self.collection.ensure_album_identity(value['browse']) if value is not None else None
            for video in dict.fromkeys(value.get('videos', []) if value is not None else ()):
                if not self.repository.video_exists(video):
                    self.tracks.observe_track_entry(video, '', '', preferred_key='@unresolved-video:' + video)
            return self.repository.put_album_context(value, album_id)

    def bind_album(self, pending, account_id):
        with self.guard():
            if self.repository.album_context_available():
                return self.repository.bind_album_context(pending, account_id)
            return self.remember_album({**pending, 'account_id': account_id})

    def bind_choice(self, account_id, playlist, video, opportunity, option, now, window_s):
        with self.guard():
            if self.repository.playback_bindings_available():
                if not self.repository.video_exists(video):
                    self.tracks.observe_track_entry(video, '', '', preferred_key='@unresolved-video:' + video)
                return self.repository.put_choice_binding(account_id, playlist, video, opportunity, str(option), now,
                                                          before=now - window_s, limit=BINDING_LIMIT)
            rows = self._bindings()
            key = account_id, playlist, video
            rows = [row for row in rows if 0 <= now - row['at'] <= window_s
                    and (row['account_id'], row['playlist'], row['video']) != key]
            rows.append({'account_id': account_id, 'playlist': playlist, 'video': video,
                         'opportunity_id': opportunity, 'option_id': str(option), 'at': now})
            self.settings.set_setting(BINDINGS_KEY,
                json.dumps([entity_names.external_account_document(row) for row in rows[-BINDING_LIMIT:]]))

    def choice(self, account_id, playlist, video, now, window_s):
        if self.repository.playback_bindings_available():
            return self.repository.choice_binding(account_id, playlist, video, before=now - window_s, through=now)
        return next((row for row in reversed(self._bindings())
                     if (row['account_id'], row['playlist']) == (account_id, playlist)
                     and (video is None or row['video'] == video)
                     and 0 <= now - row['at'] <= window_s), None)

    def _bindings(self):
        return [entity_names.account_document(row) for row in
                json.loads(self.settings.get_setting(BINDINGS_KEY) or '[]')]
