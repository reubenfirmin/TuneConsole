"""Task 6 (#50): once the library enrich queue is caught up, drain_once enriches a batch of the
discovered (cold) pool through a DiscoveredSink, newest pulls first."""
from types import SimpleNamespace

from yt_playlist.enrich.enrich_worker import EnrichWorker


def test_drain_processes_discovered_when_library_caught_up(monkeypatch, store):
    store.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    monkeypatch.setattr(store, "next_enrich_batch", lambda n: [], raising=False)   # library caught up
    monkeypatch.setattr(store, "resweep_batch", lambda n, b: [], raising=False)
    seen = {}

    def fake_waterfall(s, batch, cfg, on_progress, should_stop=None, sink_for=None):
        seen["keys"] = [t["identity_key"] for t in batch]
        for t in batch:                                  # simulate a provider writing genre via the sink
            sink_for(t).set_enrichment("techno", "2020")

    ctx = SimpleNamespace(store=store, now_fn=lambda: 500.0,
                          logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
    w = EnrichWorker(ctx, waterfall_fn=fake_waterfall)
    n = w.drain_once(limit=10)
    assert n == 1 and seen["keys"] == ["a|x"]
    assert store.discovered_tracks_by_keys(["a|x"])["a|x"]["genre"] == "techno"
    # probed once: the row is now stamped, so the next drain finds nothing and returns 0
    assert w.drain_once(limit=10) == 0
