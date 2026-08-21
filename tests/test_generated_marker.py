# tests/test_generated_marker.py
"""The generated-playlist quarantine is carried in the playlist's YouTube DESCRIPTION.

Which group a playlist is in lives in the local database, so it doesn't exist on your other machine
- or after a reinstall. Those installs sync the same account, see the app's own generated playlists
as ordinary library playlists, and start reading its suggestions back as your taste. The YouTube
account is the one thing every install shares, so the marker lives there and sync reconciles from it.
"""
from yt_playlist.library import executor, sync
from yt_playlist.core.store import Store
from yt_playlist.repos.base import GENERATED_GROUP
from tests.conftest import FakeClient, _track


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


class _DescClient(FakeClient):
    """A FakeClient whose get_playlist also returns a description, as the real API does."""

    def __init__(self, descriptions=None, **kw):
        super().__init__(**kw)
        self._descriptions = descriptions or {}

    def get_playlist(self, playlistId, limit=100):
        out = super().get_playlist(playlistId, limit)
        out["description"] = self._descriptions.get(playlistId)
        return out


def _sync(store, client):
    iid = store.upsert_identity("main", "cred", None, True)
    sync.sync_identity(store, iid, client, now=1000.0)
    return iid


def test_a_generated_playlist_syncs_into_quarantine_on_a_fresh_machine():
    """The case that started this: a second install syncs the same account and has never been told
    these are the app's own playlists."""
    s = _store()
    client = _DescClient(
        playlists=[{"playlistId": "PLGEN", "title": "Road Trip: Beach Run", "count": 1},
                   {"playlistId": "PLMINE", "title": "My Mix", "count": 1}],
        tracks={"PLGEN": [_track("v1", "A", "X")], "PLMINE": [_track("v2", "B", "Y")]},
        descriptions={"PLGEN": executor.GENERATED_DESCRIPTION, "PLMINE": "my own playlist"})

    _sync(s, client)

    groups = s.get_playlist_groups()
    assert groups.get("PLGEN") == GENERATED_GROUP     # recognised from the account, not local state
    assert groups.get("PLMINE") is None               # an ordinary playlist is left alone


def test_promoting_elsewhere_releases_it_here():
    """Promotion rewrites the description upstream, so the next sync on any other machine agrees."""
    s = _store()
    s.set_playlist_group("PLGEN", GENERATED_GROUP)
    client = _DescClient(
        playlists=[{"playlistId": "PLGEN", "title": "Road Trip: Beach Run", "count": 1}],
        tracks={"PLGEN": [_track("v1", "A", "X")]},
        descriptions={"PLGEN": executor.PROMOTED_DESCRIPTION})

    _sync(s, client)

    assert s.get_playlist_groups().get("PLGEN") is None


def test_a_group_you_chose_is_never_overwritten():
    """Only the Generated group is managed from the marker; your own filing is yours."""
    s = _store()
    s.set_playlist_group("PLGEN", "Road trips")
    client = _DescClient(
        playlists=[{"playlistId": "PLGEN", "title": "Road Trip: Beach Run", "count": 1}],
        tracks={"PLGEN": [_track("v1", "A", "X")]},
        descriptions={"PLGEN": executor.GENERATED_DESCRIPTION})

    _sync(s, client)

    assert s.get_playlist_groups().get("PLGEN") == "Road trips"


def test_a_playlist_with_no_description_is_left_alone():
    s = _store()
    client = _DescClient(
        playlists=[{"playlistId": "PLMINE", "title": "My Mix", "count": 1}],
        tracks={"PLMINE": [_track("v1", "A", "X")]},
        descriptions={})

    _sync(s, client)

    assert s.get_playlist_groups() == {}


def test_creating_a_generated_playlist_writes_the_marker():
    s = _store()
    iid = s.upsert_identity("main", "cred", None, True)
    client = FakeClient()

    executor.create_generated_playlist(
        s, "Road Trip: Beach Run",
        [{"video_id": "v1", "title": "A", "artist": "X", "album": "", "thumbnail": None,
          "duration": 200}], client, 1000.0, iid)

    assert client.created[0][2] == executor.GENERATED_DESCRIPTION
    assert executor.is_generated_description(client.created[0][2])
    assert not executor.is_generated_description(executor.PROMOTED_DESCRIPTION)
