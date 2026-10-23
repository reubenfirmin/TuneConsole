"""Library and discovery memberships use the same catalog enrichment sink."""
from yt_playlist.core.store import Store
from yt_playlist.providers import waterfall
from yt_playlist.providers.base import EnrichmentResult


class _FakeProvider:
    name = "fake"
    def reset(self): pass
    def tripped(self): return False
    def available(self, store): return True
    def probe(self, t, store):
        return EnrichmentResult(provider="fake",
                                fields={"genre": "techno", "year": "2019", "bpm": 128.0, "label": "x"})


def test_catalog_sink_retains_discovery_findings_and_full_answers():
    s = Store(":memory:"); s.init_schema()
    s.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    cfg = [{"name": "fake", "enabled": True, "label": "Fake"}]
    completed = waterfall.run_waterfall(
        s, s.enrichment_queue.next_enrich_batch(10), cfg, on_progress=lambda e: None,
        registry={"fake": _FakeProvider()})
    row = s.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "techno" and row["year"] == "2019"
    assert row["audio"]["bpm"] == 128.0
    assert completed == [s.tracks.catalog_subject(1)["recording_id"]]
    assert s.enrichment_state.enrichment_attempts("a|x")["fake"]["fields"]["label"] == "x"
    assert s.conn.execute("SELECT label FROM catalog_tracks").fetchone()[0] == "x"
