"""One queue enriches library and discovery tracks, then places only completed recordings."""
from functools import partial
import logging
from types import SimpleNamespace

import pytest

from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall


def test_drain_processes_discovered(store):
    store.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None, None, None, "radio:s", 100.0)
    seen = {}

    def fake_waterfall(s, batch, cfg, on_progress, should_stop=None, sink_for=None, **kwargs):
        seen["keys"] = [t["identity_key"] for t in batch]
        for t in batch:                                  # simulate a provider writing genre via the sink
            sink_for(t).set_enrichment("techno", "2020")

    ctx = SimpleNamespace(store=store, now_fn=lambda: 500.0,
                          logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
    w = EnrichWorker(ctx, waterfall_fn=fake_waterfall)
    n = w.drain_once(limit=10)
    assert n == 1 and seen["keys"] == ["a|x"]
    assert store.discovery.discovered_tracks_by_keys(["a|x"])["a|x"]["genre"] == "techno"
    # probed once: the row is now stamped, so the next drain finds nothing and returns 0
    assert w.drain_once(limit=10) == 0


def test_mixed_batch_follows_shared_order_and_refreshes_fresh(monkeypatch, store):
    from yt_playlist.rec import content_updates

    store.settings.set_setting("enrich_caught_up_at", "50")
    store.tracks.upsert_track("older", "Older library song", "Artist", None, 200, created_at=1.0)
    newest = store.tracks.upsert_track("newest", "Newest library song", "Artist", None, 200, created_at=200.0)
    store.discovery.upsert_discovered_track("new|artist", "new", "New", "Artist", None, None,
                                  None, None, "radio:seed", 100.0)
    seen, prepared = [], []
    def update(s, keys, now):
        prepared.append((set(keys), now,
            s.discovery.discovered_tracks_by_keys(["new|artist"])["new|artist"]))

    monkeypatch.setattr(content_updates, "update_tracks", update)

    def waterfall(s, batch, cfg, on_progress, should_stop=None, sink_for=None, **kwargs):
        seen.append([track["title"] for track in batch])
        for track in batch:
            sink_for(track).set_enrichment("techno", "2024")
            sink_for(track).set_audio(bpm=128.0, energy=.8, danceability=.7)

    ctx = SimpleNamespace(store=store, now_fn=lambda: 500.0,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    worker = EnrichWorker(ctx, waterfall_fn=waterfall)
    assert worker.drain_once(limit=2) == 2
    assert seen == [["Newest library song", "New"]]
    assert store.enrichment.queue_remaining() == 1
    assert store.tracks.get_track_enrichment(newest) == ("techno", "2024")
    keys, now, discovery = prepared[0]
    assert keys == {"newest library song|artist", "new|artist"} and now == 500.0
    assert discovery["genre"] == "techno" and discovery["year"] == "2024"
    assert discovery["audio"] == {"bpm": 128.0, "energy": .8, "danceability": .7}
    assert worker.drain_once(limit=1) == 1
    assert seen[-1] == ["Older library song"]
    assert prepared[1][0] == {"older library song|artist"}
    assert store.enrichment.queue_remaining() == 0


@pytest.mark.parametrize("interrupt", [None, "pause", "error", "breaker"])
def test_mixed_waterfall_commits_each_track_and_keeps_unfinished_work(store, monkeypatch, interrupt):
    from yt_playlist.rec import embed, fresh_pool

    monkeypatch.setattr(embed, "maybe_rebuild_content_vectors", lambda s: None)
    monkeypatch.setattr(embed, "build_discovered_content_vectors", lambda s: None)
    monkeypatch.setattr(fresh_pool, "prepare", lambda s, now: None)
    store.settings.set_setting("enrich_caught_up_at", "1")
    library = store.tracks.upsert_track("lib", "Library", "Artist", None, 200, created_at=200.0)
    store.discovery.upsert_discovered_track("new|artist", "new", "New", "Artist", None, None,
                                  None, None, "radio:seed", 300.0)
    ctx = SimpleNamespace(store=store, now_fn=lambda: 500.0, logger=logging.getLogger("test"))
    worker = EnrichWorker(ctx)

    class Provider:
        name = "musicbrainz"
        def available(self, _store): return True
        def reset(self): self.dead = False
        def tripped(self): return self.dead
        def probe(self, track, _store):
            if track["title"] == "New":
                if interrupt == "pause":
                    store.settings.set_setting("enrich_worker_enabled", "0")
                if interrupt == "breaker":
                    self.dead = True
            else:
                # The discovery completion must already be visible before the library lookup.
                assert store.enrichment.queue_remaining() == 1
                assert store.enrichment.enriched_tracks()[0]["title"] == "New"
                if interrupt == "error":
                    raise RuntimeError("provider unavailable")
            return EnrichmentResult(self.name, {"genre": "techno", "year": "2024", "bpm": 128.0})

    worker.waterfall_fn = partial(run_waterfall, registry={"musicbrainz": Provider()})
    if interrupt == "error":
        assert worker.drain_once() == 1
        assert "provider unavailable" in worker.activity["warning"]
    else:
        assert worker.drain_once() == (0 if interrupt == "breaker" else (1 if interrupt else 2))
    assert store.enrichment.queue_remaining() == (2 if interrupt == "breaker" else (1 if interrupt else 0))
    assert store.discovery.discovered_tracks_by_keys(["new|artist"])["new|artist"]["genre"] == "techno"
    pending = store.enrichment.next_enrich_batch(10)
    if interrupt == "breaker":
        assert {track["title"] for track in pending} == {"New", "Library"}
    else:
        assert [track["id"] for track in pending] == ([library] if interrupt else [])
    assert not worker.busy
