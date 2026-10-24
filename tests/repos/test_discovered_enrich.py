"""Discovery persistence and participation in the shared enrichment queue."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_batch_is_newest_first_and_excludes_enriched():
    s = _store()
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.discovery.upsert_discovered_track("b|y", "v2", "B", "Y", None, None, None, None, "radio:s", 200.0)
    batch = s.enrichment_queue.next_enrich_batch(10)
    assert [r["identity_key"] for r in batch] == ["b|y", "a|x"]   # found_at DESC
    assert batch[0]["video_id"] == "v2" and batch[0]["artist"] == "Y" and batch[0]["title"] == "B"
    s.discovery.mark_discovered_enriched(["b|y"], 300.0)
    assert [r["identity_key"] for r in s.enrichment_queue.next_enrich_batch(10)] == ["a|x"]


def test_set_audio_writes_known_columns_and_ignores_extras():
    s = _store()
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.discovery.set_discovered_enrichment("a|x", "techno", "2019")
    s.discovery.set_discovered_audio("a|x", bpm=128.0, energy=0.9, popularity=999, label="ignored")
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "techno" and row["year"] == "2019"
    assert row["audio"]["bpm"] == 128.0 and row["audio"]["energy"] == 0.9
    assert "popularity" not in row["audio"] and "label" not in row["audio"]


def test_set_enrichment_is_fill_only():
    s = _store()
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, "house", "2001", "radio:s", 100.0)
    s.discovery.set_discovered_enrichment("a|x", None, "2002")        # None genre must not wipe existing
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "house" and row["year"] == "2002"


def test_rediscovery_preserves_enrichment_when_catalog_metadata_is_missing():
    s = _store()
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.discovery.set_discovered_enrichment("a|x", "techno", "2020")
    s.discovery.set_discovered_audio("a|x", bpm=128.0, energy=.8)
    s.discovery.mark_discovered_enriched(["a|x"], 200.0)
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", "Album", None, None, None, "album", 300.0)
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "techno" and row["year"] == "2020"
    assert row["audio"] == {"bpm": 128.0, "energy": .8}
    assert s.enrichment_reports.coverage_stats(include_discovery=True)["processed"] == 1


def test_legacy_discovery_stamps_are_requeued_once_without_metadata_changes(tmp_path, legacy_store):
    path = tmp_path / "legacy.db"
    s = legacy_store
    with s.conn:
        s.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key) VALUES ('library','Library','Artist','library|artist')")
        s.conn.execute("INSERT INTO discovered_tracks(identity_key,video_id,title,artist,genre,year,source_browse_id,found_at,bpm) VALUES ('cold','v1','Cold','Artist','House','2020','radio:seed',100,128)")
        s.conn.execute('DROP TABLE schema_migrations')
    s.discovery.mark_discovered_enriched(["cold"], 200)
    s.settings.delete_setting("discovery_enrichment_audit_requeued")  # simulate a pre-repair database
    s.conn.close()
    repaired = Store(path)
    repaired.init_schema()
    assert repaired.enrichment_reports.queue_remaining() == 2
    row = repaired.discovery.discovered_tracks_by_keys(["cold"])["cold"]
    assert row["last_enriched"] is None
    assert (row["genre"], row["year"], row["audio"]) == ("House", "2020", {"bpm": 128.0})
    repaired.enrichment_state.record_enrichment_attempt("cold", "deezer", {}, now=300)
    repaired.discovery.mark_discovered_enriched(["cold"], 300)
    repaired.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    assert reopened.enrichment_reports.queue_remaining() == 1
    assert reopened.enrichment_state.enrichment_attempts("cold")["deezer"]["status"] == "no_match"
    reopened.conn.close()
