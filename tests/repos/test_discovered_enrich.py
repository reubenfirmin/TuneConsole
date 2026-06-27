"""Task 2 (#50): the cold-enrichment queue over discovered_tracks (newest pull first, probe once) plus
the fill-only genre/year/audio writers."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_batch_is_newest_first_and_excludes_enriched():
    s = _store()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    s.upsert_discovered_track("b|y", "v2", "B", "Y", None, None, None, None, "radio:s", 200.0)
    batch = s.next_discovered_enrich_batch(10)
    assert [r["identity_key"] for r in batch] == ["b|y", "a|x"]   # found_at DESC
    assert batch[0]["video_id"] == "v2" and batch[0]["artist"] == "Y" and batch[0]["title"] == "B"
    s.mark_discovered_enriched(["b|y"], 300.0)
    assert [r["identity_key"] for r in s.next_discovered_enrich_batch(10)] == ["a|x"]


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
