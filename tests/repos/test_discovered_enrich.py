"""Discovery persistence and participation in the shared enrichment queue."""
from contextlib import closing

from yt_playlist.core.store import Store
from tests.enrichment_support import complete_discovered


def test_batch_is_newest_first_and_excludes_enriched(store):
    s = store
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.discovery.upsert_discovered_track("b|y", "v2", "B", "Y", None, None, None, None, "radio:s", 200.0)
    batch = s.enrichment_queue.next_enrich_batch(10)
    assert [r["identity_key"] for r in batch] == ["b|y", "a|x"]   # found_at DESC
    assert batch[0]["video_id"] == "v2" and batch[0]["artist"] == "Y" and batch[0]["title"] == "B"
    s.enrichment_queue.mark_recordings_enriched([batch[0]['recording_id']], 300.0)
    assert [r["identity_key"] for r in s.enrichment_queue.next_enrich_batch(10)] == ["a|x"]


def test_provider_metadata_reaches_discovery_audio_projection(store):
    s = store
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    entry = s.discovery.discovered_tracks_by_keys(['a|x'])['a|x']['id']
    s.enrichment.apply_provider_fields(entry, 'deezer', {
        'genre': 'techno', 'year': '2019', 'bpm': 128.0, 'energy': .9,
        'popularity': 999, 'label': 'Because Music'})
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "Techno" and row["year"] == "2019"
    assert row["audio"]["bpm"] == 128.0 and row["audio"]["energy"] == 0.9
    assert "popularity" not in row["audio"] and "label" not in row["audio"]


def test_provider_refresh_preserves_genre_when_new_value_is_missing(store):
    s = store
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, "house", "2001", "radio:s", 100.0)
    entry = s.discovery.discovered_tracks_by_keys(['a|x'])['a|x']['id']
    s.enrichment.apply_provider_fields(entry, 'musicbrainz', {'genre': None, 'year': '2002'})
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "House" and row["year"] == "2002"


def test_rediscovery_preserves_enrichment_when_catalog_metadata_is_missing(store):
    s = store
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    entry = s.discovery.discovered_tracks_by_keys(['a|x'])['a|x']['id']
    s.enrichment.apply_provider_fields(entry, 'deezer', {'genre': 'techno', 'year': '2020', 'bpm': 128.0, 'energy': .8})
    complete_discovered(s, ['a|x'], 200.0)
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", "Album", None, None, None, "album", 300.0)
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "Techno" and row["year"] == "2020"
    assert row["audio"] == {"bpm": 128.0, "energy": .8}
    assert s.enrichment_reports.coverage_stats(include_discovery=True)["processed"] == 1


def test_legacy_discovery_stamps_are_requeued_once_without_metadata_changes(tmp_path, legacy_store):
    path = tmp_path / "legacy.db"
    s = legacy_store
    with s.conn:
        s.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key) VALUES ('library','Library','Artist','library|artist')")
        s.conn.execute("INSERT INTO discovered_tracks(identity_key,video_id,title,artist,genre,year,source_browse_id,found_at,bpm) VALUES ('cold','v1','Cold','Artist','House','2020','radio:seed',100,128)")
        s.conn.execute('DROP TABLE schema_migrations')
    with s.conn:
        s.conn.execute('UPDATE discovered_tracks SET last_enriched=? WHERE identity_key=?', (200, 'cold'))
    s.settings.delete_setting("discovery_enrichment_audit_requeued")  # simulate a pre-repair database
    s.conn.close()
    repaired = Store(path)
    with closing(repaired.conn):
        from tests.historical_support import upgrade_fixture
        upgrade_fixture(repaired)
        assert repaired.enrichment_reports.queue_remaining() == 2
        row = repaired.discovery.discovered_tracks_by_keys(["cold"])["cold"]
        assert row["last_enriched"] is None
        assert (row["genre"], row["year"], row["audio"]) == ("House", "2020", {"bpm": 128.0})
        repaired.enrichment_state.record_enrichment_attempt("cold", "deezer", {}, now=300)
        complete_discovered(repaired, ['cold'], 300)
    reopened = Store(path)
    with closing(reopened.conn):
        reopened.init_schema()
        assert reopened.enrichment_reports.queue_remaining() == 1
        assert reopened.enrichment_state.enrichment_attempts("cold")["deezer"]["status"] == "no_match"
