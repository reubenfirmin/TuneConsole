"""Task 2 (#52): TTL-since-first-shown GC of the discovery pool, with held tracks (live generated
playlists) kept, and the acquisition prune respecting the same hold."""
from yt_playlist.core.store import Store


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def test_gc_deletes_aged_first_shown_keeps_unshown_and_held():
    s = _s()
    day = 86400.0
    for k in ("old|x", "new|y", "held|z", "unshown|w"):
        s.upsert_discovered_track(k, "v", k[0], k[0], None, None, None, None, "r", 0.0)
    now = 100 * day
    s.mark_offered("track", ["old|x", "held|z"], now - 40 * day)   # first_shown 40d ago -> aged
    s.mark_offered("track", ["new|y"], now - 5 * day)              # first_shown 5d ago -> fresh
    # "unshown|w" never offered -> first_shown NULL -> never GC'd by the clock
    out = s.gc_discovery_pool(now, gc_days=30, held_track_keys={"held|z"})
    keys = {r["identity_key"] for r in s.get_discovered_tracks()}
    assert keys == {"new|y", "held|z", "unshown|w"}
    assert out["tracks"] == 1


def test_gc_albums_and_artists_by_clock():
    s = _s()
    day = 86400.0
    s.upsert_discovered_album("b1", "A", "Alb", "2020", None, 0.0, genre=None)
    s.upsert_discovered_artist("Art", 0.5, None, None, None, 0.0, genre=None)
    now = 100 * day
    s.mark_offered("album", ["b1"], now - 40 * day)
    s.mark_offered("artist", ["Art"], now - 40 * day)
    out = s.gc_discovery_pool(now, gc_days=30, held_track_keys=set())
    assert out["albums"] == 1 and out["artists"] == 1
    assert s.get_discovered_albums() == [] and s.get_discovered_artists() == []


def test_prune_keeps_held_generated_keys():
    s = _s()
    s.upsert_discovered_track("g|x", "v", "A", "X", None, None, None, None, "r", 0.0)
    s.prune_discovered_tracks({"g|x"}, held_keys={"g|x"})         # in library but held -> kept
    assert {r["identity_key"] for r in s.get_discovered_tracks()} == {"g|x"}
    s.prune_discovered_tracks({"g|x"})                            # no hold -> pruned
    assert s.get_discovered_tracks() == []
