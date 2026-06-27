"""Task 3 (#50): run_waterfall persists through a pluggable sink. The default sink keeps today's
library-track behavior; DiscoveredSink routes genre/year/audio onto discovered_tracks by identity_key."""
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


def test_discovered_sink_writes_to_discovered_tracks():
    s = Store(":memory:"); s.init_schema()
    s.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    cfg = [{"name": "fake", "enabled": True, "label": "Fake"}]
    waterfall.run_waterfall(
        s, s.next_discovered_enrich_batch(10), cfg, on_progress=lambda e: None,
        registry={"fake": _FakeProvider()},
        sink_for=lambda t: waterfall.DiscoveredSink(s, t["identity_key"]))
    row = s.discovered_tracks_by_keys(["a|x"])["a|x"]
    assert row["genre"] == "techno" and row["year"] == "2019"
    assert row["audio"]["bpm"] == 128.0
    assert "label" not in row["audio"]                 # no such discovered_tracks column
