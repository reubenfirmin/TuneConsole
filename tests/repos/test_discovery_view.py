"""Task 6 (#53): per-pool view methods feeding the Tools screen (stats + GC date)."""
from yt_playlist.core.store import Store


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def test_track_view_has_stats_and_gc_at():
    s = _s()
    s.upsert_discovered_track("a|x", "v1", "Song", "Artist", "Alb", None, "house", "2020", "r", 100.0)
    s.mark_offered("track", ["a|x"], 200.0)
    rows = {r["identity_key"]: r for r in s.discovery_track_view(1000.0, gc_days=30)}
    r = rows["a|x"]
    assert r["found_at"] == 100.0 and r["offered_count"] == 1
    assert r["gc_at"] == 200.0 + 30 * 86400
    assert r["plays"] == 0 and r["playlists"] == 0 and r["title"] == "Song"


def test_unshown_track_has_null_gc_at():
    s = _s()
    s.upsert_discovered_track("a|x", "v1", "Song", "Artist", "Alb", None, None, None, "r", 100.0)
    r = s.discovery_track_view(1000.0, gc_days=30)[0]
    assert r["gc_at"] is None              # never shown -> no GC clock


def test_album_and_artist_views():
    s = _s()
    s.upsert_discovered_album("b1", "Artist", "Album", "2020", None, 100.0, genre="house")
    s.upsert_discovered_artist("Artist", 0.5, None, None, None, 100.0, genre="house")
    s.mark_offered("album", ["b1"], 200.0)
    s.mark_offered("artist", ["Artist"], 200.0)
    alb = s.discovery_album_view(1000.0, gc_days=30)[0]
    art = s.discovery_artist_view(1000.0, gc_days=30)[0]
    assert alb["title"] == "Album" and alb["offered_count"] == 1 and alb["gc_at"] == 200.0 + 30 * 86400
    assert art["artist"] == "Artist" and art["offered_count"] == 1 and art["gc_at"] == 200.0 + 30 * 86400
