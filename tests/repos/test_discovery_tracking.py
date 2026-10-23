"""Task 1 (#52/#53): engagement tracking on the discovery pools (first_shown set once, offered_count
bumped, last_shown updated) via the unified mark_offered."""
from yt_playlist.core.store import Store


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def test_mark_offered_track_sets_first_shown_once_and_bumps_count():
    s = _s()
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.discovery.mark_offered("track", ["a|x"], 200.0)
    r = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert r["first_shown"] == 200.0 and r["last_shown"] == 200.0 and r["offered_count"] == 1
    s.discovery.mark_offered("track", ["a|x"], 300.0)
    r = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert r["first_shown"] == 200.0                  # unchanged on a later offer
    assert r["last_shown"] == 300.0 and r["offered_count"] == 2


def test_pick_albums_does_not_count_an_impression():
    from yt_playlist.rec import discover
    s = _s()
    s.discovery.upsert_discovered_album("b1", "Artist", "Album", "2020", None, 100.0, genre=None)
    discover.pick_discovered_albums(s, 5, 200.0)
    alb = next(a for a in s.discovery.get_discovered_albums() if a["browse_id"] == "b1")
    assert alb["offered_count"] == 0 and alb["first_shown"] is None


def test_visible_impressions_are_deduplicated_per_visit():
    s = _s()
    s.discovery.upsert_discovered_album("b1", "Artist", "Album", "2020", None, 100.0)
    s.discovery.record_discovery_impressions("album", ["b1", "b1", "unknown"], "visit1", 200.0)
    s.discovery.record_discovery_impressions("album", ["b1"], "visit1", 300.0)
    alb = s.discovery.get_discovered_albums()[0]
    assert alb["offered_count"] == 1 and alb["first_shown"] == 200.0
    s.discovery.record_discovery_impressions("album", ["b1"], "visit2", 400.0)
    alb = s.discovery.get_discovered_albums()[0]
    assert alb["offered_count"] == 2 and alb["last_shown"] == 400.0
    assert s.conn.execute("SELECT COUNT(*) FROM discovery_visible_impressions").fetchone()[0] == 2


def test_mark_offered_album_and_artist():
    s = _s()
    s.discovery.upsert_discovered_album("b1", "Artist", "Album", "2020", None, 100.0, genre="house")
    s.discovery.upsert_discovered_artist("Artist", 0.5, None, None, None, 100.0, genre="house")
    s.discovery.mark_offered("album", ["b1"], 200.0)
    s.discovery.mark_offered("artist", ["Artist"], 200.0)
    alb = next(a for a in s.discovery.get_discovered_albums() if a["browse_id"] == "b1")
    art = next(a for a in s.discovery.get_discovered_artists() if a["artist"] == "Artist")
    assert alb["first_shown"] == 200.0 and alb["offered_count"] == 1
    assert art["first_shown"] == 200.0 and art["offered_count"] == 1


def test_legacy_server_counts_are_preserved_without_starting_expiry(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE discovered_albums (
                browse_id TEXT PRIMARY KEY, artist TEXT, title TEXT, year TEXT, thumbnail TEXT,
                found_at REAL, last_shown REAL, genre TEXT, first_shown REAL,
                offered_count INTEGER NOT NULL DEFAULT 0);
            INSERT INTO discovered_albums VALUES
                ('album', 'Rita Payés', 'Rueda Eterna', '2026', NULL, 100, 300, NULL, 200, 18);
        """)
    s = Store(path)
    s.init_schema()
    album = s.discovery.discovery_album_view(400.0, 30)[0]
    assert album["legacy_offered_count"] == 18
    assert album["offered_count"] == 0 and album["first_shown"] is None and album["gc_at"] is None
    s.discovery.record_discovery_impressions("album", ["album"], "visit", 500.0)
    s.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    album = reopened.discovery.discovery_album_view(600.0, 30)[0]
    assert album["legacy_offered_count"] == 18 and album["offered_count"] == 1
    assert album["first_shown"] == 500.0
    reopened.conn.close()
