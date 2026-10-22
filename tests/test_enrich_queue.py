"""Enrichment worker priority queue + coverage/stats queries."""


def _track(store, vid, title, artist, **kw):
    return store.tracks.upsert_track(vid, title, artist, kw.get("album"), 200,
                              album_browse_id=kw.get("album_browse_id"),
                              created_at=kw.get("created_at"))


def _play(store, identity_key, n=1):
    # Enrichment uses listening-day counts, independently of exact replay frequency.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    for day in range(n):
        store.history.record_history_plays(iid,day*86400+43200,[identity_key],source_timezone='UTC')


def _names(batch):
    return [b["title"] for b in batch]


def test_tiers_ordered_new_played_container_orphan(store):
    from yt_playlist.util.matching import identity_key
    iid = store.identities.upsert_identity("main", "cred", None, True)
    # a played song (tier 1)
    played = _track(store, "v1", "Played", "A", created_at=10.0)
    _play(store, identity_key("Played", "A"), 5)
    # a zero-play song that shares a playlist with the played one -> tier 2 (container has plays)
    inpl = _track(store, "v2", "InPlayedPlaylist", "B", created_at=10.0)
    pl = store.playlists.upsert_playlist(iid, "PL", "Mix", 2, "h", 1.0)
    store.playlists.set_playlist_tracks(pl, [played, inpl])
    # an orphan (zero-play, no played container) -> tier 3
    orphan = _track(store, "v3", "Orphan", "C", created_at=10.0)
    # a brand-new arrival (created after caught-up) -> tier 0, jumps everything
    store.settings.set_setting("enrich_caught_up_at", "100.0")
    newbie = _track(store, "v4", "NewArrival", "D", created_at=200.0)

    order = _names(store.enrichment.next_enrich_batch(10))
    assert order.index("NewArrival") < order.index("Played")          # tier 0 first
    assert order.index("Played") < order.index("InPlayedPlaylist")    # tier 1 before tier 2
    assert order.index("InPlayedPlaylist") < order.index("Orphan")    # tier 2 before tier 3


def test_played_songs_sorted_by_playcount(store):
    from yt_playlist.util.matching import identity_key
    store.settings.set_setting("enrich_caught_up_at", "100")    # tracks below are not "new" -> tier 1
    a = _track(store, "v1", "Low", "X", created_at=1.0)
    b = _track(store, "v2", "High", "Y", created_at=1.0)
    _play(store, identity_key("Low", "X"), 2)
    _play(store, identity_key("High", "Y"), 9)
    assert _names(store.enrichment.next_enrich_batch(10))[:2] == ["High", "Low"]


def test_batch_excludes_processed_and_respects_limit(store):
    store.settings.set_setting("enrich_caught_up_at", "0")
    t1 = _track(store, "v1", "One", "A", created_at=1.0)
    _track(store, "v2", "Two", "B", created_at=1.0)
    store.enrichment.mark_enriched([t1], now=5.0)                  # One is now processed
    batch = store.enrichment.next_enrich_batch(10)
    assert _names(batch) == ["Two"]                     # processed track excluded
    assert len(store.enrichment.next_enrich_batch(1)) == 1         # limit honored


def test_startup_requeues_legacy_processed_rows_with_no_attempt_evidence(legacy_store):
    store = legacy_store
    with store.conn:
        tid = store.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key,created_at,first_enriched_at,last_enriched_at) VALUES ('v1','Skipped','A','skipped|a',1,5,5)").lastrowid
        store.conn.execute('DROP TABLE schema_migrations')
    store.settings.delete_setting("enrich_unattempted_batch_repaired")
    store.init_schema()
    row = store.conn.execute(
        "SELECT first_enriched_at, last_enriched_at FROM tracks WHERE id=?", (tid,)).fetchone()
    assert row["first_enriched_at"] is None and row["last_enriched_at"] is None
    assert [t["id"] for t in store.enrichment.next_enrich_batch(10)] == [tid]


def test_coverage_stats_and_queue_remaining(store):
    t1 = _track(store, "v1", "One", "A")
    _track(store, "v2", "Two", "B")
    store.tracks.set_track_enrichment(t1, "Rock", "1999")     # genre+year on one
    store.tracks.set_track_audio(t1, bpm=120.0)
    store.enrichment.mark_enriched([t1], now=5.0)
    cov = store.enrichment.coverage_stats()
    assert cov["total"] == 2 and cov["processed"] == 1
    assert cov["genre"] == 1 and cov["year"] == 1 and cov["bpm"] == 1 and cov["energy"] == 0
    assert store.enrichment.queue_remaining() == 1                # Two still unprocessed


def test_resweep_selects_stale_incomplete_only(store):
    t1 = _track(store, "v1", "Stale", "A")
    t2 = _track(store, "v2", "Fresh", "B")
    t3 = _track(store, "v3", "Complete", "C")
    store.tracks.set_track_enrichment(t3, "Rock", "1999")     # complete-ish
    store.tracks.set_track_audio(t3, bpm=120.0, energy=0.5, danceability=0.5)
    store.enrichment.mark_enriched([t1], now=1.0)                 # stale + incomplete
    store.enrichment.mark_enriched([t2], now=1000.0)              # fresh
    store.enrichment.mark_enriched([t3], now=1.0)                 # stale but complete
    sweep = _names(store.enrichment.resweep_batch(10, stale_before=500.0))
    assert sweep == ["Stale"]                          # only stale AND incomplete


def test_processed_timeline_is_cumulative(store):
    for i, ts in enumerate([10.0, 20.0, 30.0]):
        t = _track(store, f"v{i}", f"T{i}", "A")
        store.enrichment.mark_enriched([t], now=ts)
    tl = store.enrichment.processed_timeline(buckets=3)
    assert tl[-1]["n"] == 3                            # cumulative reaches total
    assert [p["n"] for p in tl] == sorted(p["n"] for p in tl)  # monotonic


def test_one_priority_order_applies_to_both_sources_before_limit_and_offset(store):
    from yt_playlist.util.matching import identity_key

    store.settings.set_setting("enrich_caught_up_at", "100")
    _track(store, "lib-new", "Newest library", "A", created_at=400.0)
    _track(store, "lib-old", "Old library", "B", created_at=1.0)
    store.discovery.upsert_discovered_track("new|c", "disc-new", "New discovery", "C", None, None,
                                  None, None, "radio:s", 300.0)
    played_key = identity_key("Played discovery", "D")
    store.discovery.upsert_discovered_track(played_key, "disc-played", "Played discovery", "D", None, None,
                                  None, None, "radio:s", 1.0)
    _play(store, played_key, 5)
    assert _names(store.enrichment.next_enrich_batch(10)) == [
        "Newest library", "New discovery", "Played discovery", "Old library"]
    assert _names(store.enrichment.next_enrich_batch(2, 1)) == ["New discovery", "Played discovery"]
    assert store.enrichment.queue_remaining() == 4


def test_discovery_uses_artist_engagement_in_shared_priority(store):
    from yt_playlist.util.matching import identity_key

    store.settings.set_setting("enrich_caught_up_at", "100")
    played = _track(store, "played", "Played", "Artist", created_at=1.0)
    _play(store, identity_key("Played", "Artist"), 5)
    store.enrichment.mark_enriched([played], 10.0)
    _track(store, "orphan", "Library orphan", "Other", created_at=2.0)
    store.discovery.upsert_discovered_track("new|artist", "new", "Artist discovery", "Artist", None, None,
                                  None, None, "radio:s", 1.0)
    assert _names(store.enrichment.next_enrich_batch(10)) == ["Artist discovery", "Library orphan"]


def test_combined_coverage_processed_list_activity_and_timeline(store):
    library = _track(store, "lib", "Library", "A", created_at=1.0)
    store.enrichment.mark_enriched([library], 10.0)
    store.discovery.upsert_discovered_track("new|b", "new", "Discovery", "B", None, None,
                                  "techno", "2024", "radio:s", 1.0)
    store.discovery.set_discovered_audio("new|b", bpm=128.0)
    assert store.enrichment.queue_remaining() == 1
    assert store.enrichment.coverage_stats(include_discovery=True) == {
        "total": 2, "processed": 1, "genre": 1, "year": 1, "bpm": 1, "energy": 0, "danceability": 0}
    store.discovery.mark_discovered_enriched(["new|b"], 20.0)
    assert store.enrichment.queue_remaining() == 0
    assert [track["title"] for track in store.enrichment.enriched_tracks()] == ["Discovery", "Library"]
    assert store.enrichment.enrichment_activity_stats(1000.0) == {"latest": 20.0, "recent": 0}
    assert store.enrichment.enrichment_activity_stats(100.0) == {"latest": 20.0, "recent": 2}
    assert store.enrichment.processed_timeline()[-1]["n"] == 2
    # Onboarding's existing library-coverage query must not change with the discovery pool.
    assert store.enrichment.coverage_stats()["total"] == 1


def test_resweep_selects_both_sources_by_oldest_check(store):
    library = _track(store, "lib", "Library", "A")
    store.enrichment.mark_enriched([library], 20.0)
    for title, timestamp in (("Stale", 10.0), ("Fresh", 1000.0), ("Complete", 1.0)):
        store.discovery.upsert_discovered_track(title, title, title, "B", None, None,
                                      None, None, "radio:s", 1.0)
        store.discovery.mark_discovered_enriched([title], timestamp)
    store.discovery.set_discovered_enrichment("Complete", "techno", "2024")
    store.discovery.set_discovered_audio("Complete", bpm=128.0, energy=.8, danceability=.7)
    assert _names(store.enrichment.resweep_batch(10, stale_before=500.0)) == ["Stale", "Library"]
    assert store.enrichment.resweep_batch(1, stale_before=500.0)[0]["identity_key"] == "Stale"
