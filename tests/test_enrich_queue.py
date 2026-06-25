"""Enrichment worker priority queue + coverage/stats queries."""


def _track(store, vid, title, artist, **kw):
    return store.upsert_track(vid, title, artist, kw.get("album"), 200,
                              album_browse_id=kw.get("album_browse_id"),
                              created_at=kw.get("created_at"))


def _play(store, identity_key, n=1):
    # record n plays for a song (history_items rows under a snapshot); playcount = COUNT(*) by key
    iid = store.upsert_identity("main", "cred", None, True)
    sid = store.conn.execute("INSERT INTO history_snapshots(identity_id, taken_at) "
                             "VALUES (?, 1.0)", (iid,)).lastrowid
    for _ in range(n):
        store.conn.execute("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?,?)",
                           (sid, identity_key))
    store.conn.commit()


def _names(batch):
    return [b["title"] for b in batch]


def test_tiers_ordered_new_played_container_orphan(store):
    from yt_playlist.util.matching import identity_key
    iid = store.upsert_identity("main", "cred", None, True)
    # a played song (tier 1)
    played = _track(store, "v1", "Played", "A", created_at=10.0)
    _play(store, identity_key("Played", "A"), 5)
    # a zero-play song that shares a playlist with the played one -> tier 2 (container has plays)
    inpl = _track(store, "v2", "InPlayedPlaylist", "B", created_at=10.0)
    pl = store.upsert_playlist(iid, "PL", "Mix", 2, "h", 1.0)
    store.set_playlist_tracks(pl, [played, inpl])
    # an orphan (zero-play, no played container) -> tier 3
    orphan = _track(store, "v3", "Orphan", "C", created_at=10.0)
    # a brand-new arrival (created after caught-up) -> tier 0, jumps everything
    store.set_setting("enrich_caught_up_at", "100.0")
    newbie = _track(store, "v4", "NewArrival", "D", created_at=200.0)

    order = _names(store.next_enrich_batch(10))
    assert order.index("NewArrival") < order.index("Played")          # tier 0 first
    assert order.index("Played") < order.index("InPlayedPlaylist")    # tier 1 before tier 2
    assert order.index("InPlayedPlaylist") < order.index("Orphan")    # tier 2 before tier 3


def test_played_songs_sorted_by_playcount(store):
    from yt_playlist.util.matching import identity_key
    store.set_setting("enrich_caught_up_at", "100")    # tracks below are not "new" -> tier 1
    a = _track(store, "v1", "Low", "X", created_at=1.0)
    b = _track(store, "v2", "High", "Y", created_at=1.0)
    _play(store, identity_key("Low", "X"), 2)
    _play(store, identity_key("High", "Y"), 9)
    assert _names(store.next_enrich_batch(10))[:2] == ["High", "Low"]


def test_batch_excludes_processed_and_respects_limit(store):
    store.set_setting("enrich_caught_up_at", "0")
    t1 = _track(store, "v1", "One", "A", created_at=1.0)
    _track(store, "v2", "Two", "B", created_at=1.0)
    store.mark_enriched([t1], now=5.0)                  # One is now processed
    batch = store.next_enrich_batch(10)
    assert _names(batch) == ["Two"]                     # processed track excluded
    assert len(store.next_enrich_batch(1)) == 1         # limit honored


def test_coverage_stats_and_queue_remaining(store):
    t1 = _track(store, "v1", "One", "A")
    _track(store, "v2", "Two", "B")
    store.set_track_enrichment(t1, "Rock", "1999")     # genre+year on one
    store.set_track_audio(t1, bpm=120.0)
    store.mark_enriched([t1], now=5.0)
    cov = store.coverage_stats()
    assert cov["total"] == 2 and cov["processed"] == 1
    assert cov["genre"] == 1 and cov["year"] == 1 and cov["bpm"] == 1 and cov["energy"] == 0
    assert store.queue_remaining() == 1                # Two still unprocessed


def test_resweep_selects_stale_incomplete_only(store):
    t1 = _track(store, "v1", "Stale", "A")
    t2 = _track(store, "v2", "Fresh", "B")
    t3 = _track(store, "v3", "Complete", "C")
    store.set_track_enrichment(t3, "Rock", "1999")     # complete-ish
    store.set_track_audio(t3, bpm=120.0, energy=0.5, danceability=0.5)
    store.mark_enriched([t1], now=1.0)                 # stale + incomplete
    store.mark_enriched([t2], now=1000.0)              # fresh
    store.mark_enriched([t3], now=1.0)                 # stale but complete
    sweep = _names(store.resweep_batch(10, stale_before=500.0))
    assert sweep == ["Stale"]                          # only stale AND incomplete


def test_processed_timeline_is_cumulative(store):
    for i, ts in enumerate([10.0, 20.0, 30.0]):
        t = _track(store, f"v{i}", f"T{i}", "A")
        store.mark_enriched([t], now=ts)
    tl = store.processed_timeline(buckets=3)
    assert tl[-1]["n"] == 3                            # cumulative reaches total
    assert [p["n"] for p in tl] == sorted(p["n"] for p in tl)  # monotonic
