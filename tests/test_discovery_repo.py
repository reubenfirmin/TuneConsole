"""DiscoveryRepo: interested-artist ranking, stale-scan selection, and the accumulating pools."""


def _artist_with(store, iid, name, plays=0, playlists=0, saved=0):
    t = store.upsert_track(f"v_{name}", f"S_{name}", name, None, None)
    for _ in range(plays):
        store.add_history_snapshot(iid, 1.0, [f"s_{name.lower()}|{name.lower()}"])
    for i in range(playlists):
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PL_{name}_{i}", "p", 1, "h", 0.0), [t])
    for i in range(saved):
        store.save_album(f"alb_{name}_{i}", f"Alb {i}", name, "2020", "album", None) \
            if hasattr(store, "save_album") else None


def test_interested_artists_ranks_by_combined_signal(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Big: in 3 playlists; Small: 1 play only
    big = store.upsert_track("v1", "S1", "Big", None, None)
    for i in range(3):
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PB{i}", "p", 1, "h", 0.0), [big])
    store.upsert_track("v2", "S2", "Small", None, None)
    store.add_history_snapshot(iid, 1.0, ["s2|small"])
    ranked = store.interested_artists()
    names = [r["artist"] for r in ranked]
    assert "Big" in names and "Small" in names
    assert names.index("Big") < names.index("Small")        # 3 playlists (×2) beats 1 play (×1)


def test_artists_due_for_scan_skips_fresh_and_honors_ttl(store):
    iid = store.upsert_identity("main", "cred", None, True)
    for n in ("A", "B"):
        t = store.upsert_track(f"v{n}", f"S{n}", n, None, None)
        store.set_playlist_tracks(store.upsert_playlist(iid, f"P{n}", "p", 1, "h", 0.0), [t])
    now = 1000.0 * 86400
    assert set(store.artists_due_for_scan(now, ttl_days=5, budget=10)) == {"A", "B"}   # never scanned
    store.mark_scanned("A", now)
    assert store.artists_due_for_scan(now, ttl_days=5, budget=10) == ["B"]              # A is fresh
    assert "A" in store.artists_due_for_scan(now + 6 * 86400, ttl_days=5, budget=10)    # 6 days -> due again


def test_artists_due_for_scan_respects_budget(store):
    iid = store.upsert_identity("main", "cred", None, True)
    for n in range(5):
        t = store.upsert_track(f"v{n}", f"S{n}", f"Art{n}", None, None)
        store.set_playlist_tracks(store.upsert_playlist(iid, f"P{n}", "p", 1, "h", 0.0), [t])
    assert len(store.artists_due_for_scan(1.0e9, ttl_days=5, budget=2)) == 2


def test_discovered_albums_accumulate_not_overwrite(store):
    store.upsert_discovered_album("b1", "Art", "Alb One", "2024", None, now=1.0)
    store.upsert_discovered_album("b1", "Art", "Alb One", "2024", None, now=2.0)   # same -> still one
    store.upsert_discovered_album("b2", "Art", "Alb Two", "2019", None, now=3.0)
    albums = {a["browse_id"]: a for a in store.get_discovered_albums()}
    assert set(albums) == {"b1", "b2"}
    assert albums["b1"]["found_at"] == 1.0                  # first-seen preserved across re-upsert


def test_pick_discovered_albums_biases_recent_mixes_older_and_avoids_repeats(store):
    from yt_playlist.rec import discover
    for i in range(5):
        store.upsert_discovered_album(f"new{i}", "A", f"New {i}", "2025", None, now=1.0)
    for i in range(5):
        store.upsert_discovered_album(f"old{i}", "A", f"Old {i}", "2009", None, now=1.0)
    picks = discover.pick_discovered_albums(store, n=4, now=10.0, recent_frac=0.7)
    yrs = [p["year"] for p in picks]
    assert len(picks) == 4
    assert yrs.count("2025") >= 2 and yrs.count("2009") >= 1     # recency-biased, but older mixed in
    # a just-shown album is de-prioritized vs a never-shown one of the same year
    store.upsert_discovered_album("shown", "A", "Shown", "2025", None, now=1.0)
    store.mark_shown("album", ["shown"], now=10.0)
    store.upsert_discovered_album("nevr", "A", "Never", "2025", None, now=1.0)
    again = {p["browse_id"] for p in discover.pick_discovered_albums(store, n=6, now=20.0)}
    assert "nevr" in again            # never-shown surfaces; "shown" is pushed down by last_shown


def test_pick_discovered_albums_varies_artists(store):
    from yt_playlist.rec import discover
    # Six distinct artists, plus a second album from Art0, the "mixed" vs "split" pair of one release.
    for i in range(6):
        store.upsert_discovered_album(f"b{i}", f"Art{i}", f"Alb {i}", "2025", None, now=1.0)
    store.upsert_discovered_album("b0-mixed", "Art0", "Alb 0 (Mixed)", "2025", None, now=1.0)
    picks = discover.pick_discovered_albums(store, n=5, now=10.0)
    arts = [p["artist"] for p in picks]
    assert len(picks) == 5
    assert len(set(arts)) == 5         # one album per artist while the pool can supply distinct ones
    assert arts.count("Art0") == 1     # the mixed + split pair never both surface


def test_pick_discovered_albums_repeats_artist_only_when_forced(store):
    from yt_playlist.rec import discover
    # Just two artists but n=4 -> must reuse artists to fill, rather than return fewer than asked.
    for a in ("X", "Y"):
        for i in range(3):
            store.upsert_discovered_album(f"{a}{i}", a, f"{a} {i}", "2025", None, now=1.0)
    picks = discover.pick_discovered_albums(store, n=4, now=10.0)
    assert len(picks) == 4
    assert len({p["browse_id"] for p in picks}) == 4   # distinct albums even though artists repeat


class _Ctx:
    def __init__(self, store):
        self.store = store
        self.now_fn = lambda: 1.0e9
        self.client_provider = lambda: {1: object()}
        import logging
        self.logger = logging.getLogger("test")


def test_run_discovery_accumulates_and_skips_fresh(store, monkeypatch):
    from yt_playlist.rec import discover
    iid = store.upsert_identity("main", "cred", None, True)
    for n in ("A", "B"):
        t = store.upsert_track(f"v{n}", f"S{n}", n, None, None)
        store.set_playlist_tracks(store.upsert_playlist(iid, f"P{n}", "p", 1, "h", 0.0), [t])
    # fake the network: each artist has one unowned album; no new-artist fetch
    monkeypatch.setattr(discover, "fetch_artist_info",
                        lambda ctx, name, browse_id=None: {"albums": [
                            {"browse_id": f"alb_{name}", "title": f"{name} Album", "year": "2024", "thumbnail": None}]})
    monkeypatch.setattr(discover, "new_artists", lambda ctx: [])
    ctx = _Ctx(store)
    discover.run_discovery(ctx, now=1.0e9, budget=10)
    got = {a["browse_id"] for a in store.get_discovered_albums()}
    assert got == {"alb_A", "alb_B"}                       # both scanned, albums pooled
    assert set(store.artists_due_for_scan(1.0e9, budget=10)) == set()   # both now fresh
    # a later pass past the TTL re-scans without duplicating the pool
    discover.run_discovery(ctx, now=1.0e9 + 6 * 86400, budget=10)
    assert len(store.get_discovered_albums()) == 2          # accumulate, not duplicate
