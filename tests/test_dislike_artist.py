"""#54: disliking one song by an artist must not suppress all of that artist's songs."""
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import transient


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_dislike_does_not_emit_artist_lean(store, monkeypatch):
    store.conn.execute(
        "INSERT INTO tracks (identity_key, title, artist, genre, mb_year) VALUES (?, ?, ?, ?, ?)",
        ("billy|rebel", "Rebel Yell", "Billy Idol", "rock-classic", "1983"))
    store.conn.commit()
    store.record_dislike("billy|rebel", None, 100.0)
    # #85: staleness_factor is gone; facet_leans decays each event on its own wall clock and never
    # depended on a global sync-staleness relax, so there is nothing left to isolate from here.

    leans = transient.facet_leans(store, now=100.0)

    # The artist is NOT pushed negative by a single disliked track (the bug).
    assert all(v >= 0.0 for f, v in leans.items() if f.startswith("artist:")), leans
    # But the dislike still registers as a (broad, non-artist) negative signal, so it isn't inert.
    assert any(v < 0.0 for f, v in leans.items() if not f.startswith("artist:")), leans
