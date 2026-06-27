"""Repo primitives for bounding the album pool (#52): top-N interest restriction, per-artist
delete, orphaned-track prune."""
from yt_playlist.core.store import Store


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def test_interested_artists_limit():
    s = _s()
    # two artists with different engagement: A (2 plays), B (1 play)
    iid = s.upsert_identity("m", "c", None, True)
    s.upsert_track("v1", "S1", "A", None, None)
    s.upsert_track("v2", "S2", "A", None, None)
    s.upsert_track("v3", "S3", "B", None, None)
    s.add_history_snapshot(iid, 1.0, ["s1|a", "s2|a", "s3|b"])
    s.add_history_snapshot(iid, 2.0, ["s1|a"])
    top = s.interested_artists(limit=1)
    assert len(top) == 1 and top[0]["artist"] == "A"     # most-engaged only


def test_delete_discovered_albums_and_artist_query():
    s = _s()
    for i in range(3):
        s.upsert_discovered_album(f"k{i}", "Keep", f"T{i}", "2000", None, 0.0)
    assert set(s.discovered_albums_for_artist("Keep")) == {"k0", "k1", "k2"}
    s.delete_discovered_albums(["k0", "k2"])
    assert set(s.discovered_albums_for_artist("Keep")) == {"k1"}


def test_prune_orphan_discovered_tracks_keeps_radio_and_live_albums():
    s = _s()
    s.upsert_discovered_album("alb1", "A", "T", "2000", None, 0.0)
    s.upsert_discovered_track("t|1", "v", "T", "A", "Alb", None, None, None, "alb1", 0.0)   # source exists
    s.upsert_discovered_track("t|2", "v", "T2", "A", "Alb", None, None, None, "gone", 0.0)  # source gone
    s.upsert_discovered_track("r|3", "v", "R", "A", None, None, None, None, "radio:vid", 0.0)  # radio
    s.prune_orphan_discovered_tracks()
    keys = {r["identity_key"] for r in s.get_discovered_tracks()}
    assert keys == {"t|1", "r|3"}                         # orphan album-track gone; radio kept
