"""EnrichWorker drain logic, exercised synchronously via drain_once with a fake waterfall."""
import logging
from functools import partial
from types import SimpleNamespace

import pytest
from unittest.mock import Mock

from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall


def make_worker(store, now=1000.0, fill=None):
    calls = []

    def fake_wf(s, tracks, config, on_progress, should_stop=None, sink_for=None):
        calls.append([t["id"] for t in tracks])
        for t in tracks:
            if fill:
                fill(s, t)

    ctx = SimpleNamespace(store=store, now_fn=lambda: now, logger=logging.getLogger("test"))
    w = EnrichWorker(ctx, waterfall_fn=fake_wf)
    w.calls = calls
    return w


def _unprocessed(store, vid="v1", title="S", artist="A"):
    store.set_setting("enrich_caught_up_at", "100")        # created_at below -> not "new"
    return store.upsert_track(vid, title, artist, None, 200, created_at=1.0)


def test_drain_processes_batch_and_marks_timestamps(store):
    t = _unprocessed(store)
    w = make_worker(store, now=1000.0)
    assert w.drain_once(limit=10) == 1
    assert w.calls == [[t]]                                 # waterfall got the batch
    row = store.conn.execute(
        "SELECT first_enriched_at f, last_enriched_at l FROM tracks WHERE id=?", (t,)).fetchone()
    assert row["f"] == 1000.0 and row["l"] == 1000.0
    assert store.queue_remaining() == 0                    # nothing left unprocessed


def test_stamps_caught_up_after_primary_drains(store):
    _unprocessed(store)
    w = make_worker(store, now=1000.0)
    w.drain_once()                                         # drains the one track (_had_work=True)
    assert w.drain_once() == 0                             # primary empty now
    assert float(store.get_setting("enrich_caught_up_at")) == 1000.0


def test_paused_drain_does_not_mark(store):
    t = _unprocessed(store)
    store.set_setting("enrich_worker_enabled", "0")        # paused
    w = make_worker(store, now=1000.0)
    assert w.drain_once() == 0                             # should_stop -> batch abandoned
    assert store.queue_remaining() == 1                    # left for next time, not marked


def test_resweep_reprocesses_stale_incomplete(store):
    t = store.upsert_track("v1", "S", "A", None, 200, created_at=1.0)
    store.set_setting("enrich_caught_up_at", "100")
    store.set_setting("enrich_resweep_days", "0")          # everything older than 'now' is stale
    store.mark_enriched([t], now=1.0)                      # processed, still missing all fields
    w = make_worker(store, now=1000.0)
    assert store.next_enrich_batch(10) == []               # not in primary (already processed)
    assert w.drain_once() == 1                             # picked up by re-sweep
    assert w.calls == [[t]]
    assert store.conn.execute(
        "SELECT last_enriched_at l FROM tracks WHERE id=?", (t,)).fetchone()["l"] == 1000.0


def test_enrichment_fills_via_waterfall_are_persisted(store):
    t = _unprocessed(store)
    # a fake waterfall that actually fills genre, like the real one would
    w = make_worker(store, now=1000.0, fill=lambda s, tr: s.set_track_enrichment(tr["id"], "Rock", "1999"))
    w.drain_once()
    assert store.coverage_stats()["genre"] == 1


@pytest.mark.parametrize("rebuilt", [False, True])
def test_library_enrichment_schedules_genre_group_catchup(store, monkeypatch, rebuilt):
    from yt_playlist.rec import embed
    _unprocessed(store)
    worker = make_worker(store)
    worker.ctx.rec_worker = SimpleNamespace(trigger_mode_refresh=Mock())
    monkeypatch.setattr(embed, "maybe_rebuild_content_vectors", lambda s: rebuilt)
    assert worker.drain_once() == 1
    worker.ctx.rec_worker.trigger_mode_refresh.assert_called_once_with(force=rebuilt)


def test_tracks_skipped_by_waterfall_remain_in_primary_queue(store):
    first = _unprocessed(store, "v1", "One")
    second = store.upsert_track("v2", "Two", "A", None, 200, created_at=1.0)
    w = make_worker(store, now=1000.0)
    w.waterfall_fn = lambda *_args, **_kwargs: [first]
    assert w.drain_once(limit=10) == 1
    assert store.conn.execute(
        "SELECT first_enriched_at FROM tracks WHERE id=?", (first,)).fetchone()[0] == 1000.0
    assert store.conn.execute(
        "SELECT first_enriched_at FROM tracks WHERE id=?", (second,)).fetchone()[0] is None
    assert [t["id"] for t in store.next_enrich_batch(10)] == [second]


@pytest.mark.parametrize("interrupt", [None, "pause", "error"])
def test_real_waterfall_commits_each_track_before_next_lookup(store, interrupt):
    first = _unprocessed(store, "v1", "One")
    second = _unprocessed(store, "v2", "Two")
    worker = make_worker(store)

    class Provider:
        name = "musicbrainz"
        def available(self, _store): return True
        def reset(self): pass
        def tripped(self): return False
        def probe(self, track, _store):
            assert worker.busy
            assert track["title"] in worker.activity["text"]
            if track["id"] == first and interrupt == "pause":
                store.set_setting("enrich_worker_enabled", "0")
            if track["id"] == second:
                # This is still inside the waterfall: the batch hasn't returned yet.
                assert store.queue_remaining() == 1
                assert store.enriched_tracks()[0]["id"] == first
                if interrupt == "error":
                    raise RuntimeError("provider unavailable")
            return EnrichmentResult(self.name, {})  # no match is still a completed check

    worker.waterfall_fn = partial(run_waterfall, registry={"musicbrainz": Provider()})
    if interrupt == "error":
        with pytest.raises(RuntimeError, match="provider unavailable"):
            worker.drain_once()
        assert worker.activity["state"] == "error"
    else:
        assert worker.drain_once() == (1 if interrupt == "pause" else 2)
    assert not worker.busy
    assert store.queue_remaining() == (1 if interrupt else 0)
    assert store.coverage_stats()["genre"] == 0


def test_breaker_skipped_tracks_stay_queued_with_live_warning(store):
    first = _unprocessed(store, "v1", "One")
    second = _unprocessed(store, "v2", "Two")
    worker = make_worker(store)

    class Provider:
        name = "musicbrainz"
        def available(self, _store): return True
        def reset(self): self.dead = False
        def tripped(self): return self.dead
        def probe(self, track, _store):
            self.dead = True
            return EnrichmentResult(self.name, {})

    worker.waterfall_fn = partial(run_waterfall, registry={"musicbrainz": Provider()})
    assert worker.drain_once() == 0
    assert store.enriched_tracks() == []
    assert {t["id"] for t in store.next_enrich_batch(10)} == {first, second}
    assert "unreachable" in worker.activity["warning"]
