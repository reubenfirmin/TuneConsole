"""EnrichWorker drain logic — exercised synchronously via drain_once with a fake waterfall."""
import logging
from types import SimpleNamespace

from yt_playlist.enrich.enrich_worker import EnrichWorker


def make_worker(store, now=1000.0, fill=None):
    calls = []

    def fake_wf(s, tracks, config, on_progress, should_stop=None):
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
