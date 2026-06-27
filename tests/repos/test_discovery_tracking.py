"""Task 1 (#52/#53): engagement tracking on the discovery pools (first_shown set once, offered_count
bumped, last_shown updated) via the unified mark_offered."""
from yt_playlist.core.store import Store


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def test_mark_offered_track_sets_first_shown_once_and_bumps_count():
    s = _s()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.mark_offered("track", ["a|x"], 200.0)
    r = s.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert r["first_shown"] == 200.0 and r["last_shown"] == 200.0 and r["offered_count"] == 1
    s.mark_offered("track", ["a|x"], 300.0)
    r = s.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert r["first_shown"] == 200.0                  # unchanged on a later offer
    assert r["last_shown"] == 300.0 and r["offered_count"] == 2


def test_pick_albums_marks_offered(monkeypatch):
    from yt_playlist.rec import discover
    s = _s()
    s.upsert_discovered_album("b1", "Artist", "Album", "2020", None, 100.0, genre=None)
    monkeypatch.setattr(discover, "_discovery_facet_w", lambda store, genre, now: 1.0)
    discover.pick_discovered_albums(s, 5, 200.0)
    alb = next(a for a in s.get_discovered_albums() if a["browse_id"] == "b1")
    assert alb["offered_count"] == 1 and alb["first_shown"] == 200.0


def test_mark_offered_album_and_artist():
    s = _s()
    s.upsert_discovered_album("b1", "Artist", "Album", "2020", None, 100.0, genre="house")
    s.upsert_discovered_artist("Artist", 0.5, None, None, None, 100.0, genre="house")
    s.mark_offered("album", ["b1"], 200.0)
    s.mark_offered("artist", ["Artist"], 200.0)
    alb = next(a for a in s.get_discovered_albums() if a["browse_id"] == "b1")
    art = next(a for a in s.get_discovered_artists() if a["artist"] == "Artist")
    assert alb["first_shown"] == 200.0 and alb["offered_count"] == 1
    assert art["first_shown"] == 200.0 and art["offered_count"] == 1
