"""Task 2 (#52): TTL-since-first-shown GC of the discovery pool, with held tracks (live generated
playlists) kept, and the acquisition prune respecting the same hold."""
from yt_playlist.core.store import Store


def _s():
    s = Store(":memory:"); s.init_schema(); return s


def test_gc_expires_aged_unshown_tracks_and_keeps_recent_and_held():
    s = _s()
    day = 86400.0
    for k in ("old|x", "new|y", "held|z", "unshown|w"):
        s.discovery.upsert_discovered_track(k, "v:" + k, k[0], k[0], None, None, None, None, "r", 0.0)
    now = 100 * day
    s.discovery.mark_offered("track", ["old|x", "held|z"], now - 40 * day)   # first_shown 40d ago -> aged
    s.discovery.mark_offered("track", ["new|y"], now - 5 * day)              # first_shown 5d ago -> fresh
    s.discovery.upsert_discovered_track("recent|r", "v2", "Recent", "R", None, None, None, None,
                              "radio:seed", now - 5 * day)
    out = s.discovery.gc_discovery_pool(now, gc_days=30, held_track_keys={"held|z"})
    keys = {r["identity_key"] for r in s.discovery.get_discovered_tracks()}
    assert keys == {"new|y", "held|z", "recent|r"}
    assert out["tracks"] == 2


def test_gc_albums_and_artists_by_clock():
    s = _s()
    day = 86400.0
    s.discovery.upsert_discovered_album("b1", "A", "Alb", "2020", None, 0.0, genre=None)
    s.discovery.upsert_discovered_artist("Art", 0.5, None, None, None, 0.0, genre=None)
    now = 100 * day
    s.discovery.mark_offered("album", ["b1"], now - 40 * day)
    s.discovery.mark_offered("artist", ["Art"], now - 40 * day)
    out = s.discovery.gc_discovery_pool(now, gc_days=30, held_track_keys=set())
    assert out["albums"] == 1 and out["artists"] == 1
    assert s.discovery.get_discovered_albums() == [] and s.discovery.get_discovered_artists() == []


def test_prune_keeps_held_generated_keys():
    s = _s()
    s.discovery.upsert_discovered_track("g|x", "v", "A", "X", None, None, None, None, "r", 0.0)
    s.discovery.prune_discovered_tracks({"g|x"}, held_keys={"g|x"})         # in library but held -> kept
    assert {r["identity_key"] for r in s.discovery.get_discovered_tracks()} == {"g|x"}
    s.discovery.prune_discovered_tracks({"g|x"})                            # no hold -> pruned
    assert s.discovery.get_discovered_tracks() == []


def test_gc_removes_stale_tracks_from_cached_fresh_and_mode_recommendations():
    s = _s()
    for key, found in (("old", 0), ("held", 0), ("recent", 99 * 86400)):
        s.discovery.upsert_discovered_track(key, "v:" + key, key, "Artist", None, None, "House", "2020", "album", found)
    items = [{"key": key, "title": key} for key in ("old", "held", "recent")]
    s.rec_proposals.put_proposals("fresh_pool", items, 1)
    s.rec_proposals.put_proposals("mode_bundles", {"all": {"fresh": items, "wheelhouse": [{"key": "library"}]},
                                      "123": {"fresh": items}}, 1)
    s.enrichment_state.record_enrichment_attempt("old", "musicbrainz", {}, now=1)
    s.discovery.gc_discovery_pool(100 * 86400, 30, {"held"})
    assert {t["key"] for t in s.rec_proposals.get_proposals("fresh_pool")} == {"held", "recent"}
    bundles = s.rec_proposals.get_proposals("mode_bundles")
    assert {t["key"] for t in bundles["all"]["fresh"]} == {"held", "recent"}
    assert {t["key"] for t in bundles["123"]["fresh"]} == {"held", "recent"}
    assert bundles["all"]["wheelhouse"] == [{"key": "library"}]
    assert s.settings.get_setting("enrich_attempts:old") is None


def test_orphan_cleanup_preserves_tracks_in_generated_playlists():
    s = _s()
    for key in ("held", "unheld"):
        s.discovery.upsert_discovered_track(key, "v:" + key, key, "Artist", None, None, None, None, "gone-album", 0)
    assert s.discovery.prune_orphan_discovered_tracks(held_keys={"held"}) == 1
    assert set(s.discovery.discovered_tracks_by_keys(["held", "unheld"])) == {"held"}
