from yt_playlist.library.sync import sync_identity, sync_all, content_hash
from yt_playlist.library import sync as sync_mod
from tests.conftest import FakeClient, _track


# The message ytmusicapi raises when YouTube serves the SIGNED-OUT layout (singleColumnBrowseResultsRenderer
# with a "Sign in" button / signInEndpoint) instead of the signed-in twoColumnBrowseResultsRenderer. The
# full response dict is interpolated into the exception text, so the sign-in markers are present in str(e).
_SIGNED_OUT_MSG = (
    "Unable to find 'twoColumnBrowseResultsRenderer' using path "
    "['contents', 'twoColumnBrowseResultsRenderer', 'tabs', 0] on "
    "{'singleColumnBrowseResultsRenderer': {'tabs': [{'tabRenderer': {'content': "
    "{'sectionListRenderer': {'contents': [{'messageRenderer': {'text': {'runs': "
    "[{'text': 'Looking for what you’ve liked?'}]}, 'button': {'buttonRenderer': "
    "{'text': {'runs': [{'text': 'Sign in'}]}, 'navigationEndpoint': "
    "{'signInEndpoint': {'hack': True}}}}}}]}}}}]}}, exception: 'twoColumnBrowseResultsRenderer'"
)


def test_is_auth_error_detects_signed_out_response():
    """A signed-out session surfaces as a parse failure whose text carries the sign-in markers; it must
    be classified as an auth error so the user gets the re-auth banner (not a silent 'unavailable')."""
    assert sync_mod._is_auth_error(Exception(_SIGNED_OUT_MSG)) is True


def test_is_auth_error_still_matches_http_codes_and_ignores_transient():
    assert sync_mod._is_auth_error(Exception("HTTP 401 Unauthorized")) is True
    assert sync_mod._is_auth_error(Exception("403 Forbidden")) is True
    assert sync_mod._is_auth_error(Exception("temporary network blip")) is False


def test_sync_identity_soft_skips_on_bridge_disconnect(store):
    """A BridgeError (extension disconnected / no signed-in tab) is a CONNECTION problem, not a dead
    session: sync_identity must skip quietly, NOT flag re-auth, and NOT raise."""
    from yt_playlist.core.bridge import BridgeError
    iid = store.upsert_identity("main", "cred", None, True)

    class _DisconnectedClient(FakeClient):
        def get_library_playlists(self, limit=25):
            raise BridgeError("no extension connected")

    expired = {}
    sync_identity(store, iid, _DisconnectedClient(), now=1500.0,
                  on_auth_expired=lambda i, label: expired.__setitem__(i, label or str(i)))
    assert expired == {}                       # NOT flagged for re-auth
    assert store.get_playlists() == []         # nothing pruned/changed


def test_sync_all_records_last_sync_at(store):
    iid = store.upsert_identity("main", "cred", None, True)
    client = FakeClient(playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}],
                        tracks={"PL1": [_track("v1", "A", "X")]})
    sync_all(store, {iid: client}, now=1234.0)
    assert store.get_setting("last_sync_at") == "1234.0"


def test_sync_status_uses_most_recent_of_either_sync(store):
    """The 'Last synced' badge reflects the most recent sync of either kind: a recent plays/auto sync
    must not be eclipsed by an older full sync (and resets staleness too)."""
    from yt_playlist.rec.recommend import sync_status
    now = 100_000.0
    store.set_setting("last_sync_at", str(now - 17 * 3600))        # full sync 17h ago
    store.set_setting("last_plays_sync_at", str(now - 2 * 3600))   # plays synced 2h ago
    st = sync_status(store, now)
    assert st.last_synced_ago == "2 hours ago"   # not "17 hours ago"
    assert st.stale is False and st.message is None


def test_content_hash_is_order_independent():
    assert content_hash(["a", "b"]) == content_hash(["b", "a"])
    assert content_hash(["a", "b"]) != content_hash(["a", "c"])

def test_sync_identity_populates_store(store):
    iid = store.upsert_identity("main", "cred", None, True)
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Mix", "count": 2}],
        tracks={"PL1": [_track("v1", "Song A", "Artist"), _track("v2", "Song B", "Artist")]},
        history=[_track("v1", "Song A", "Artist")])
    sync_identity(store, iid, client, now=1000.0)

    pls = store.get_playlists()
    assert len(pls) == 1
    assert store.get_playlist_track_keys(pls[0].id) == {"song a|artist", "song b|artist"}
    assert store.get_recent_history_keys(0.0) == {"song a|artist"}
    assert pls[0].content_hash == content_hash(["song a|artist", "song b|artist"])

def test_sync_identity_no_truncation_beyond_defaults(store):
    """Regression: sync_identity must pass limit=None so >25 playlists and >100 tracks are
    not silently truncated by ytmusicapi's defaults."""
    iid = store.upsert_identity("main", "cred", None, True)

    # 30 playlists, exceeds the get_library_playlists default of 25
    playlists = [{"playlistId": f"PL{i}", "title": f"Playlist {i}", "count": 150}
                 for i in range(30)]

    # 150 tracks in each playlist, exceeds the get_playlist default of 100
    tracks_per_pl = {
        f"PL{i}": [_track(f"v{i}_{j}", f"Song {j}", "Artist") for j in range(150)]
        for i in range(30)
    }

    client = FakeClient(playlists=playlists, tracks=tracks_per_pl, history=[])
    sync_identity(store, iid, client, now=2000.0)

    pls = store.get_playlists()
    assert len(pls) == 30, f"Expected 30 playlists but got {len(pls)} (truncation bug?)"
    for pl in pls:
        keys = store.get_playlist_track_keys(pl.id)
        assert len(keys) == 150, (
            f"Playlist {pl.remote_id} has {len(keys)} track keys, expected 150 (truncation bug?)"
        )


def test_sync_prunes_playlists_gone_from_remote(store):
    import yt_playlist.library.sync as sync
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    # stale row in the store that the remote library no longer lists
    ghost = store.upsert_playlist(iid, "GHOST", "Deleted Already", 1, "h", 1.0)
    store.set_playlist_tracks(ghost, [store.upsert_track("vx", "Old", "X", None, 1)])
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Live", "count": 1}],
        tracks={"PL1": [_track("v1", "A", "X")]})
    sync.sync_identity(store, iid, client, now=1.0)
    ytm_ids = {p.ytm_playlist_id for p in store.get_playlists()}
    assert ytm_ids == {"PL1"}                 # GHOST pruned, PL1 present
    assert store.get_playlist(ghost) is None
