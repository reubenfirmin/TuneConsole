"""Automatic retries repair failed resources without repeating healthy remote reads."""
import json
from collections import Counter

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library import sync
from yt_playlist.library.sync_outcomes import RETRY_BASE_S, sync_due
from yt_playlist.web.app import _background_sync_loop


class CountedClient(FakeClient):
    def __init__(self, count=1):
        super().__init__(playlists=[{"playlistId": f"PL{i}", "title": f"List {i}"}
                                   for i in range(count)],
                         tracks={f"PL{i}": [{**_track(f"v{i}", f"Song {i}", "Artist"),
                                              "likeStatus": "INDIFFERENT"}] for i in range(count)})
        self.reads = Counter()
        self.partial = set()
        self.library_failed = False

    def get_library_playlists(self, limit=None):
        self.reads["listing"] += 1
        if self.library_failed:
            raise ValueError("playlist-library snapshot has no complete grid")
        return super().get_library_playlists(limit)

    def get_playlist(self, playlistId, limit=None):
        self.reads[playlistId] += 1
        detail = super().get_playlist(playlistId, limit)
        if playlistId in self.partial:
            detail["trackCount"] = 2
        return detail

    def get_history(self):
        self.reads["history"] += 1
        return []

    def get_library_albums(self, limit=None):
        self.reads["albums"] += 1
        return []


@pytest.fixture(autouse=True)
def no_request_retries(monkeypatch):
    monkeypatch.setattr(sync, "with_retry", lambda fn, **kw: fn())


def test_two_accounts_retry_only_four_incomplete_playlists_and_failed_listing(store, caplog):
    first = store.identities.upsert_identity("First", "bridge", None, True)
    second = store.identities.upsert_identity("Second", "bridge", "brand", False)
    good, bad = CountedClient(133), CountedClient()
    good.partial = {f"PL{i}" for i in range(129, 133)}
    bad.library_failed = True
    clients = {first: good, second: bad}
    result = sync.sync_all(store, clients, 100, automatic=True)
    assert result["status"] == "partial"
    assert result["accounts"][str(first)]["resources"]["playlists"]["refreshed"] == 129
    assert result["accounts"][str(second)]["resources"]["history"]["status"] == "complete"
    assert "advertised=2, received=1, continuation=False" in caplog.text
    assert "_retained" not in json.dumps(result)
    for now in (101, 160, 250, 399):
        assert not sync_due(store, now, 86400)
    assert sync_due(store, 400, 86400)
    result = sync.sync_all(store, clients, 400, automatic=True)
    resources = result["accounts"][str(first)]["resources"]
    assert resources["playlists"]["refreshed"] == 0
    assert resources["playlists"]["reused"] == 129
    assert resources["playlists"]["incomplete"] == 4
    assert resources["playlists"]["items"]["PL0"]["last_success_at"] == 100
    assert resources["playlists"]["items"]["PL129"]["last_success_at"] is None
    assert resources["history"]["last_attempt_at"] == 100
    assert not sync_due(store, 999, 86400)
    good.partial.clear()
    bad.library_failed = False
    result = sync.sync_all(store, clients, 1000, automatic=True)
    playlists = result["accounts"][str(first)]["resources"]["playlists"]
    assert playlists["status"] == "complete" and playlists["last_success_at"] == 1000
    assert playlists["items"]["PL0"]["last_attempt_at"] == 100
    assert playlists["items"]["PL129"]["last_success_at"] == 1000
    assert good.reads["listing"] == good.reads["history"] == good.reads["albums"] == 1
    assert all(good.reads[f"PL{i}"] == 1 for i in range(129))
    assert all(good.reads[f"PL{i}"] == 3 for i in range(129, 133))
    assert bad.reads["listing"] == 3 and bad.reads["history"] == bad.reads["albums"] == 1
    assert not sync_due(store, 1001, 86400)
    assert sync_due(store, 86500, 86400)
    # Mixed-age reads preserve neutral ratings and their old freshness. The
    # regular daily sweep reads every context together and completes recovery.
    assert result["accounts"][str(first)]["resources"]["ratings"]["last_success_at"] is None
    result = sync.sync_all(store, clients, 86500, automatic=True)
    assert result["status"] == "complete"
    assert result["accounts"][str(first)]["resources"]["ratings"]["last_success_at"] == 86500
    assert all(good.reads[f"PL{i}"] == 2 for i in range(129))


@pytest.mark.parametrize("manual", [True, False], ids=["explicit-retry", "reconnected"])
def test_explicit_sync_and_reconnection_bypass_backoff(store, manual):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = CountedClient(2)
    client.partial = {"PL1"}
    sync.sync_all(store, {iid: client}, 100, automatic=True)
    assert not sync_due(store, 101, 86400)
    client.partial.clear()
    assert sync_due(store, 101, 86400, retry_now=True)
    result = sync.sync_all(store, {iid: client}, 101, automatic=not manual, retry_now=not manual)
    assert result["accounts"][str(iid)]["resources"]["playlists"]["status"] == "complete"
    assert client.reads["PL1"] == 2
    assert client.reads["PL0"] == (2 if manual else 1)
    assert client.reads["listing"] == (2 if manual else 1)


def test_retry_deadlines_survive_new_schedule_and_keep_success_timestamps(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = CountedClient()
    sync.sync_all(store, {iid: client}, 100)
    client.partial = {"PL0"}
    result = sync.sync_all(store, {iid: client}, 200)
    item = result["accounts"][str(iid)]["resources"]["playlists"]["items"]["PL0"]
    assert item["last_success_at"] == 100
    assert item["last_attempt_at"] == 200
    assert item["retry_at"] == 200 + RETRY_BASE_S
    # Persisted state, not process-local timers, determines the next automatic attempt.
    assert not sync_due(store, 499, 86400)
    sync.sync_all(store, {iid: client}, 499, automatic=True)
    assert client.reads["PL0"] == 2
    assert sync_due(store, 500, 86400)
    sync.sync_all(store, {iid: client}, 500, automatic=True)
    assert client.reads["PL0"] == 3
    assert not sync_due(store, 1099, 86400)


def test_retaining_a_failed_listing_keeps_child_deadlines_and_blocks_ineffective_jobs(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = CountedClient(2)
    client.partial = {"PL1"}
    sync.sync_all(store, {iid: client}, 100)
    client.library_failed = True
    failed = sync.sync_all(store, {iid: client}, 200)
    original = failed["accounts"][str(iid)]["resources"]["playlists"]["items"]
    assert original["PL0"]["last_attempt_at"] == 100
    assert original["PL1"]["retry_at"] == 400
    retained = sync.sync_all(store, {iid: client}, 300, automatic=True)
    assert retained["accounts"][str(iid)]["resources"]["playlists"]["items"] == original
    # The child is due at 400 but cannot run until its listing is available.
    assert not sync_due(store, 400, 86400)
    assert sync_due(store, 500, 86400)
    assert client.reads["listing"] == 2


def test_reconnect_retry_survives_a_busy_manual_sync_lock(store, monkeypatch):
    import logging
    import threading
    from types import SimpleNamespace

    from yt_playlist.core.activity import Activity
    from yt_playlist.web.jobs import SyncJobs

    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = CountedClient(2)
    client.partial = {"PL1"}
    sync.sync_all(store, {iid: client}, 100)
    client.partial.clear()
    lock = threading.Lock()
    lock.acquire()

    class Ticks:
        count = 0

        def wait(self, timeout):
            self.count += 1
            if self.count == 2:
                lock.release()
            return self.count > 2

    monkeypatch.setattr("yt_playlist.web.app._maybe_check_update", lambda ctx: None)
    ctx = SimpleNamespace(store=store, work=Activity(), jobs=SyncJobs(), sync_lock=lock,
        client_provider=lambda: {iid: client}, now_fn=lambda: 101,
        rec_worker=None, enrich_worker=None, flag_auth_expired=None, clear_auth_expired=None,
        logger=logging.getLogger(__name__))
    _background_sync_loop(ctx, None, SimpleNamespace(connected=True, connection_id=7), stop=Ticks())
    assert ctx.jobs.get(1).done
    assert not ctx.jobs.get(2)
    assert client.reads["PL1"] == 2
    assert client.reads["PL0"] == client.reads["listing"] == 1
    assert ctx.work.wait(0)
