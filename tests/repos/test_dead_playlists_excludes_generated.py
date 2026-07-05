import pytest
from yt_playlist.core.store import Store
from yt_playlist.repos.base import GENERATED_GROUP


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_generated_playlist_not_dead_weight(store):
    ident = store.upsert_identity("main", "ref", None, True)
    # A never-listened generated playlist (0 listens) must NOT surface as dead weight.
    store.upsert_playlist(ident, "PLRADIO", "TuneConsole Radio", 0, "", 1.0)
    store.set_playlist_group("PLRADIO", GENERATED_GROUP)
    # A never-listened ordinary playlist SHOULD still surface.
    store.upsert_playlist(ident, "PLuser", "Old mix", 0, "", 1.0)
    titles = {d["title"] for d in store.trends.dead_playlists(max_listens=0)}
    assert "TuneConsole Radio" not in titles
    assert "Old mix" in titles
