"""Discovery history aggregation must never monopolize the serving connection."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import logging
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity, interrupt_query
from yt_playlist.core.store import Store
from yt_playlist.rec.rec_worker import RecWorker


@pytest.mark.parametrize("query", [
    lambda s: s.discovery.interested_artists(),
    lambda s: s.discovery.artists_due_for_scan(100),
    lambda s: s.discovery.discovery_track_view(100, 30),
    lambda s: s.charts.top_artists(),
    lambda s: s.rec_query.top_played_keys(),
    lambda s: s.history.spotify_history_stats(),
    lambda s: s.rec_query.library_artists(),
    lambda s: s.rec_query.rec_baskets(),
    lambda s: s.rec_query.rec_baskets_as_of(100),
], ids=["interest", "due", "track-view", "anchors", "seed-tracks", "spotify-stats", "library-artists",
        "embedding-baskets", "historical-baskets"])
def test_discovery_read_does_not_block_ui_and_stops_on_shutdown(tmp_path, monkeypatch, query):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    work = Activity()
    worker = RecWorker(SimpleNamespace(store=store, work=work,
        logger=logging.getLogger(__name__), now_fn=lambda: 100), debounce_s=0)
    entered = threading.Event()
    original = store.read_snapshot

    @contextmanager
    def paused_reader():
        with original() as snapshot:
            assert snapshot.store.conn is not store.conn

            def progress():
                entered.set()
                # Simulate a long statement on the real private SQLite connection.
                # Shutdown still interrupts it through the normal progress handler.
                worker._stop.wait(5)
                return interrupt_query()

            snapshot.store.conn.set_progress_handler(progress, 1)
            yield snapshot

    monkeypatch.setattr(store, "read_snapshot", paused_reader)
    monkeypatch.setattr(worker, "_rebuild", lambda: worker._stage("discovery", query, store))

    async def interaction():
        store.settings.set_setting("interactive", "saved")
        await asyncio.sleep(0)
        return store.settings.get_setting("interactive")

    with ThreadPoolExecutor(1) as ui:
        try:
            worker.trigger()
            assert entered.wait(5)
            assert ui.submit(asyncio.run, interaction()).result(2) == "saved"
            assert worker.busy
            worker.shutdown()
            worker.join(3)
            assert work.wait(0)
            assert not store.conn.in_transaction
        finally:
            worker.shutdown()
            worker.join(5)
            store.conn.close()
