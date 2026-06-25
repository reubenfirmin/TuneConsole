import sqlite3

from yt_playlist.core.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    return s


def test_discovered_genre_round_trip(tmp_path):
    s = _store(tmp_path)
    s.upsert_discovered_artist("Aphex Twin", 0.9, ["bridge"], ["fits"], "th", 100.0, genre="IDM")
    s.upsert_discovered_album("bid1", "Aphex Twin", "SAW II", "1994", "th", 100.0, genre="IDM")
    assert s.get_discovered_artists()[0]["genre"] == "IDM"
    assert s.get_discovered_albums()[0]["genre"] == "IDM"


def test_discovered_genre_defaults_none(tmp_path):
    s = _store(tmp_path)
    s.upsert_discovered_artist("Boards of Canada", 0.5, [], [], None, 1.0)
    assert s.get_discovered_artists()[0]["genre"] is None


def test_genre_migration_on_preexisting_db(tmp_path):
    """A DB whose discovered_* tables predate the genre column gets it added, losslessly."""
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE discovered_artists (artist TEXT PRIMARY KEY, score REAL, because TEXT, "
        "  fits TEXT, thumbnail TEXT, found_at REAL, last_shown REAL);"
        "CREATE TABLE discovered_albums (browse_id TEXT PRIMARY KEY, artist TEXT, title TEXT, "
        "  year TEXT, thumbnail TEXT, found_at REAL, last_shown REAL);"
        "INSERT INTO discovered_artists(artist, score) VALUES ('Old Artist', 0.3);")
    con.commit(); con.close()
    s = Store(db); s.init_schema()
    rows = s.get_discovered_artists()
    assert any(r["artist"] == "Old Artist" for r in rows)        # existing data preserved
    assert all("genre" in r for r in rows)                       # column now present


def test_artist_genres_dominant_per_artist(tmp_path):
    s = _store(tmp_path)
    i1 = s.upsert_track("v1", "T1", "Aphex Twin", None, None)
    i2 = s.upsert_track("v2", "T2", "Aphex Twin", None, None)
    i3 = s.upsert_track("v3", "T3", "Aphex Twin", None, None)
    s.set_track_genre(i1, "IDM"); s.set_track_genre(i2, "IDM"); s.set_track_genre(i3, "Ambient")
    g = s.artist_genres()
    assert g.get("Aphex Twin") == "IDM"      # dominant (2 of 3)
