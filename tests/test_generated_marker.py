# tests/test_generated_marker.py
"""The generated-playlist quarantine is carried in the playlist's YouTube DESCRIPTION.

Which group a playlist is in lives in the local database, so it doesn't exist on your other machine
- or after a reinstall. Those installs sync the same account, see the app's own generated playlists
as ordinary library playlists, and start reading its suggestions back as your taste. The YouTube
account is the one thing every install shares, so the marker lives there and sync reconciles from it.
"""
from yt_playlist.library import playlist_generated, sync, playlist_metadata
from yt_playlist.repos.base import GENERATED_GROUP
from tests.conftest import FakeClient, _track


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
    iid = store.accounts.upsert_account("main", "cred", None, True)
    sync.sync_identity(store, iid, client, now=1000.0)
    return iid


def test_a_generated_playlist_syncs_into_quarantine_on_a_fresh_machine(store):
    """The case that started this: a second install syncs the same account and has never been told
    these are the app's own playlists."""
    s = store
    client = _DescClient(
        playlists=[{"playlistId": "PLGEN", "title": "Road Trip: Beach Run", "count": 1},
                   {"playlistId": "PLMINE", "title": "My Mix", "count": 1}],
        tracks={"PLGEN": [_track("v1", "A", "X")], "PLMINE": [_track("v2", "B", "Y")]},
        descriptions={"PLGEN": playlist_metadata.GENERATED_DESCRIPTION, "PLMINE": "my own playlist"})

    _sync(s, client)

    groups = s.playlists.get_playlist_groups()
    assert groups.get("PLGEN") == GENERATED_GROUP     # recognised from the account, not local state
    assert groups.get("PLMINE") is None               # an ordinary playlist is left alone


def test_promoting_elsewhere_releases_it_here(store):
    """Promotion rewrites the description upstream, so the next sync on any other machine agrees."""
    s = store
    s.playlists.set_playlist_group("PLGEN", GENERATED_GROUP)
    client = _DescClient(
        playlists=[{"playlistId": "PLGEN", "title": "Road Trip: Beach Run", "count": 1}],
        tracks={"PLGEN": [_track("v1", "A", "X")]},
        descriptions={"PLGEN": playlist_metadata.PROMOTED_DESCRIPTION})

    _sync(s, client)

    assert s.playlists.get_playlist_groups().get("PLGEN") is None


def test_a_group_you_chose_is_never_overwritten(store):
    """Only the Generated group is managed from the marker; your own filing is yours."""
    s = store
    s.playlists.set_playlist_group("PLGEN", "Road trips")
    client = _DescClient(
        playlists=[{"playlistId": "PLGEN", "title": "Road Trip: Beach Run", "count": 1}],
        tracks={"PLGEN": [_track("v1", "A", "X")]},
        descriptions={"PLGEN": playlist_metadata.GENERATED_DESCRIPTION})

    _sync(s, client)

    assert s.playlists.get_playlist_groups().get("PLGEN") == "Road trips"


def test_a_playlist_with_no_description_is_left_alone(store):
    s = store
    client = _DescClient(
        playlists=[{"playlistId": "PLMINE", "title": "My Mix", "count": 1}],
        tracks={"PLMINE": [_track("v1", "A", "X")]},
        descriptions={})

    _sync(s, client)

    assert s.playlists.get_playlist_groups() == {}


def test_creating_a_generated_playlist_writes_the_marker(store):
    s = store
    iid = s.accounts.upsert_account("main", "cred", None, True)
    client = FakeClient(catalog={'v1': _track('v1', 'A', 'X')})

    playlist_generated.create_generated_playlist(
        s, "Road Trip: Beach Run",
        [{"video_id": "v1", "title": "A", "artist": "X", "album": "", "thumbnail": None,
          "duration": 200}], client, 1000.0, iid)

    assert client.created[0][2] == playlist_metadata.GENERATED_DESCRIPTION
    assert playlist_metadata.is_generated_description(client.created[0][2])
    assert not playlist_metadata.is_generated_description(playlist_metadata.PROMOTED_DESCRIPTION)
