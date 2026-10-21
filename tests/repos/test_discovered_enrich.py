"""Discovery persistence and participation in the shared enrichment queue."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_batch_is_newest_first_and_excludes_enriched():
    s = _store()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.upsert_discovered_track("b|y", "v2", "B", "Y", None, None, None, None, "radio:s", 200.0)
    batch = s.next_enrich_batch(10)
    assert [r["identity_key"] for r in batch] == ["b|y", "a|x"]   # found_at DESC
    assert batch[0]["video_id"] == "v2" and batch[0]["artist"] == "Y" and batch[0]["title"] == "B"
    s.mark_discovered_enriched(["b|y"], 300.0)
    assert [r["identity_key"] for r in s.next_enrich_batch(10)] == ["a|x"]


def test_set_audio_writes_known_columns_and_ignores_extras():
    s = _store()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.set_discovered_enrichment("a|x", "techno", "2019")
    s.set_discovered_audio("a|x", bpm=128.0, energy=0.9, popularity=999, label="ignored")
    row = s.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "techno" and row["year"] == "2019"
    assert row["audio"]["bpm"] == 128.0 and row["audio"]["energy"] == 0.9
    assert "popularity" not in row["audio"] and "label" not in row["audio"]


def test_set_enrichment_is_fill_only():
    s = _store()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, "house", "2001", "radio:s", 100.0)
    s.set_discovered_enrichment("a|x", None, "2002")        # None genre must not wipe existing
    row = s.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "house" and row["year"] == "2002"


def test_rediscovery_preserves_enrichment_when_catalog_metadata_is_missing():
    s = _store()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.set_discovered_enrichment("a|x", "techno", "2020")
    s.set_discovered_audio("a|x", bpm=128.0, energy=.8)
    s.mark_discovered_enriched(["a|x"], 200.0)
    s.upsert_discovered_track("a|x", "v1", "A", "X", "Album", None, None, None, "album", 300.0)
    row = s.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "techno" and row["year"] == "2020"
    assert row["audio"] == {"bpm": 128.0, "energy": .8}
    assert s.coverage_stats(include_discovery=True)["processed"] == 1


def test_legacy_discovery_stamps_are_requeued_once_without_schema_or_metadata_changes(tmp_path):
    import json
    path = tmp_path / "legacy.db"
    s = Store(path)
    s.init_schema()
    s.upsert_track("library", "Library", "Artist", None, None)
    s.upsert_discovered_track("cold", "v1", "Cold", "Artist", None, None, "House", "2020",
                              "radio:seed", 100, audio={"bpm": 128.0})
    s.mark_discovered_enriched(["cold"], 200)
    s.delete_setting("discovery_enrichment_audit_requeued")  # simulate a pre-repair database
    schema = list(s.conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"))
    schema = [row[0] for row in schema]
    s.conn.close()
    repaired = Store(path)
    repaired.init_schema()
    assert repaired.queue_remaining() == 2
    row = repaired.discovered_tracks_by_keys(["cold"])["cold"]
    assert row["last_enriched"] is None
    assert (row["genre"], row["year"], row["audio"]) == ("House", "2020", {"bpm": 128.0})
    assert [r[0] for r in repaired.conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")] == schema
    repaired.record_enrichment_attempt("cold", "deezer", {}, now=300)
    repaired.mark_discovered_enriched(["cold"], 300)
    repaired.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    assert reopened.queue_remaining() == 1
    assert json.loads(reopened.get_setting("enrich_attempts:cold"))["deezer"]["status"] == "no_match"
    reopened.conn.close()
