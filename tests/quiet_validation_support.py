"""Controlled provider failures below the real song and album operation boundaries."""
import json
from functools import partial

from tests.conftest import FakeClient
from tests.test_song_actions import seed_song_actions
from yt_playlist.library import playlist_creation, ratings
from yt_playlist.util.retry import with_retry


DIAGNOSTIC = 'internal-diagnostic-879: provider response could not be decoded'


class ActionProvider(FakeClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fault = None
        self.after = False
        self.kind = 'json'
        self.calls = []
        self.album_ratings = []
        self.saved = set()

    def fail(self):
        if self.kind == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>provider unavailable</html>', 0)
        raise ValueError(DIAGNOSTIC)

    def call(self, method, operation):
        self.calls.append(method)
        if self.fault == method and not self.after:
            self.fail()
        result = operation()
        if self.fault == method and self.after:
            self.fail()
        return result

    def get_album(self, browseId):
        return self.call('get_album', lambda: super(ActionProvider, self).get_album(browseId))

    def rate_song(self, videoId, rating):
        return self.call('rate_song', lambda: super(ActionProvider, self).rate_song(videoId, rating))

    def rate_playlist(self, playlistId, rating):
        def rate():
            self.album_ratings.append((playlistId, rating))
            if rating == 'LIKE':
                self.saved.add(playlistId)
            else:
                self.saved.discard(playlistId)
        return self.call('rate_playlist', rate)

    def create_playlist(self, title, description):
        return self.call('create_playlist', lambda: super(ActionProvider, self).create_playlist(title, description))


def prepare_quiet_actions(store, monkeypatch):
    data = seed_song_actions(store)
    provider = ActionProvider(albums=data['main']._albums)
    provider._albums['MPRE_new']['audioPlaylistId'] = 'QUIET-AUDIO'
    data['clients'][data['main_id']] = provider
    data['main'] = provider
    # Retain the actual rating retry algorithm and its three attempts. Only
    # its backoff clock is controlled; no route or operation is replaced.
    monkeypatch.setattr(ratings, 'with_retry', partial(with_retry, sleep=lambda delay: None))
    monkeypatch.setattr(playlist_creation, 'with_retry', partial(with_retry, sleep=lambda delay: None))
    return data
