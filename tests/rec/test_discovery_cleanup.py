"""rec-layer orchestration of the bounded/rotating album pool (#52): per-artist scan rotation, and the
one-time cleanup that bounds the existing pool with no network."""
import random

from yt_playlist.core.store import Store
from yt_playlist.rec import discover, rec_params


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def _info(n):
    return {"albums": [{"browse_id": f"b{i}", "title": f"T{i}", "year": f"19{90 + i}",
                        "thumbnail": None} for i in range(n)]}


def test_scan_artist_caps_and_rotates_out_shown():
    s = _s()
    discover._scan_artist_albums(s, "Art", _info(8), set(), set(), None, 1.0, per=3,
                                 rng=random.Random(0))
    pooled = s.discovered_albums_for_artist("Art")
    assert len(pooled) == 3                          # capped to per, not all 8

    shown = next(iter(pooled))
    s.mark_offered("album", [shown], 2.0)            # user saw it -> should rotate out next scan
    discover._scan_artist_albums(s, "Art", _info(8), set(), set(), None, 3.0, per=3,
                                 rng=random.Random(1))
    pooled2 = s.discovered_albums_for_artist("Art")
    assert len(pooled2) == 3 and shown not in pooled2


def test_cleanup_drops_non_top_artists_caps_and_prunes_orphans(monkeypatch):
    s = _s()
    for i in range(5):
        s.upsert_discovered_album(f"k{i}", "Keep", f"T{i}", "2000", None, 0.0)
    for i in range(2):
        s.upsert_discovered_album(f"d{i}", "Drop", f"T{i}", "2000", None, 0.0)
    s.upsert_discovered_track("t|d", "v", "T", "Drop", "Al", None, None, None, "d0", 0.0)   # orphaned after cleanup
    s.upsert_discovered_track("r|x", "v", "R", "A", None, None, None, None, "radio:v", 0.0)  # radio, kept
    monkeypatch.setattr(type(s), "interested_artists",
                        lambda self, limit=None: [{"artist": "Keep", "score": 9.0}], raising=False)
    rec_params.set_param(s, "discovery_albums_per_artist", 2)
    out = discover.cleanup_discovery_pool(s, rng=random.Random(0))
    albums = s.get_discovered_albums()
    assert {a["artist"] for a in albums} == {"Keep"} and len(albums) == 2
    keys = {r["identity_key"] for r in s.get_discovered_tracks()}
    assert "t|d" not in keys and "r|x" in keys       # orphan album-track gone, radio kept
    assert out["albums_removed"] == 5                # 2 Drop + 3 over-cap Keep
