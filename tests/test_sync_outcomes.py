"""Failed syncs preserve freshness and retry after reconnect without hiding healthy accounts."""
import json
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, _track
from yt_playlist.core.activity import Activity
from yt_playlist.core.bridge import BridgeError
from yt_playlist.library import sync
from yt_playlist.library.sync_outcomes import sync_due
from yt_playlist.rec.actions import sync_status
from yt_playlist.web.app import create_app, _background_sync_loop as automatic_sync
from yt_playlist.web.jobs import SyncJobs


class Client(FakeClient):
    problem = None
    failed_resource = None

    def __init__(self):
        super().__init__(playlists=[{"playlistId": "PL", "title": "Library"}],
                         tracks={"PL": [{**_track("video", "Song", "Artist"),
                                        "likeStatus": "INDIFFERENT"}]})

    def get_library_playlists(self, limit=None):
        if self.problem:
            raise self.problem
        return super().get_library_playlists(limit)

    def get_playlist(self, *args, **kwargs):
        if self.failed_resource == "playlists":
            raise TimeoutError("playlist unavailable")
        detail = super().get_playlist(*args, **kwargs)
        if self.failed_resource == "ratings":
            detail["tracks"] = [{**t, "likeStatus": None} for t in detail["tracks"]]
        return detail

    def get_history(self):
        if self.failed_resource == "history":
            raise TimeoutError("history unavailable")
        return []

    def get_library_albums(self, limit=None):
        if self.problem:
            raise self.problem
        if self.failed_resource == "saved_albums":
            raise TimeoutError("albums unavailable")
        return []


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    monkeypatch.setattr(sync, "with_retry", lambda fn, **kwargs: fn())


def test_disconnect_preserves_success_and_reconnect_is_due_immediately(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = Client()
    assert sync.sync_all(store, {iid: client}, 100)["status"] == "complete"
    client.problem = BridgeError("extension disconnected")
    result = sync.sync_all(store, {iid: client}, 200)
    assert result["status"] == "skipped"
    assert store.settings.get_setting("last_sync_at") == "100"
    assert store.settings.get_setting("last_sync_attempt_at") == "200"
    assert result["accounts"][str(iid)]["resources"]["playlists"]["last_success_at"] == 100
    assert sync_due(store, 201, 86400)
    client.problem = None
    assert sync.sync_all(store, {iid: client}, 201)["status"] == "complete"
    assert store.settings.get_setting("last_sync_at") == "201"
    assert not sync_due(store, 202, 86400)


@pytest.mark.parametrize("failure", ["disconnected", "auth", "missing", "unexpected"])
def test_healthy_account_cannot_hide_an_unavailable_account(store, failure):
    good = store.identities.upsert_identity("Good", "bridge", None, True)
    bad = store.identities.upsert_identity("Bad", "bridge", "brand", False)
    clients = {good: Client(), bad: Client()}
    sync.sync_all(store, clients, 100)
    if failure == "missing":
        del clients[bad]
    else:
        clients[bad].problem = {"disconnected": BridgeError("disconnected"),
                               "auth": RuntimeError("401 unauthorized"),
                               "unexpected": RuntimeError("API unavailable")}[failure]
    expired = []
    result = sync.sync_all(store, clients, 200, on_auth_expired=lambda *args: expired.append(args))
    assert result["status"] == "partial"
    assert result["accounts"][str(good)]["last_success_at"] == 200
    assert result["accounts"][str(bad)]["last_success_at"] == 100
    assert result["resources"]["saved_album_mirror"]["last_success_at"] == 100
    assert store.settings.get_setting("last_sync_at") == "100"
    assert bool(expired) == (failure == "auth")
    assert sync_due(store, 201, 86400)
    store.settings.set_setting("last_plays_sync_at", "200")
    assert sync_status(store, 201).stale
    assert "partial" in sync_status(store, 201).message


@pytest.mark.parametrize("failed_resource", ["playlists", "ratings", "history", "saved_albums"])
def test_resource_freshness_advances_independently(store, failed_resource):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = Client()
    sync.sync_all(store, {iid: client}, 100)
    client.failed_resource = failed_resource
    result = sync.sync_all(store, {iid: client}, 200)
    assert result["status"] == "partial"
    resources = result["accounts"][str(iid)]["resources"]
    assert resources[failed_resource]["last_success_at"] == 100
    assert resources[failed_resource]["last_attempt_at"] == 200
    healthy = "history" if failed_resource != "history" else "playlists"
    assert resources[healthy]["last_success_at"] == 200
    assert store.settings.get_setting("last_sync_at") == "100"
    assert json.loads(store.settings.get_setting("library_sync_state")) == result


def test_no_clients_is_skipped_and_an_interrupted_attempt_is_due(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    assert sync.sync_all(store, {}, 100)["status"] == "skipped"
    assert store.settings.get_setting("last_sync_at") is None
    sync.sync_all(store, {iid: Client()}, 200)
    store.settings.set_setting("last_sync_attempt_at", "201")
    assert sync_due(store, 202, 86400)


@pytest.mark.parametrize("album", [{}, {"tracks": []}, {"tracks": [{"title": "Song"}],
                                                     "trackCount": 2},
                                  {"tracks": [{"title": "Song"}], "continuation": "next"}])
def test_incomplete_album_tracks_cannot_refresh_the_library(store, album):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)

    class Albums(Client):
        def get_library_albums(self, limit=None):
            return [{"browseId": "ALBUM", "title": "Album", "artists": [{"name": "Artist"}]}]

        def get_album(self, browse_id):
            return album

    result = sync.sync_all(store, {iid: Albums()}, 100)
    assert result["resources"]["saved_album_mirror"]["status"] == "complete"
    assert result["resources"]["album_tracks"]["status"] == "failed"
    assert result["status"] == "partial"
    assert store.settings.get_setting("last_sync_at") is None


def test_production_playlist_listing_rejects_partial_pagination():
    from tests.test_saved_album_snapshot import _first_page, _page, _production_client, _renderer

    first = _page(continuation="next")
    first["items"] = [{"createPlaylistRenderer": {}}, _renderer("VLPLAYLIST")]
    client, bridge = _production_client([_first_page(first), {}])
    with pytest.raises(ValueError, match="grid"):
        client.get_library_playlists(limit=None)
    assert len(bridge.calls) == 2


def test_production_playlist_listing_reads_all_pages():
    from tests.test_saved_album_snapshot import _first_page, _page, _production_client, _renderer

    first = _page(continuation="next")
    first["items"] = [{"createPlaylistRenderer": {}}, _renderer("VLONE")]
    client, bridge = _production_client([_first_page(first),
        {"continuationContents": {"gridContinuation": _page("VLTWO")}}])
    assert [p["playlistId"] for p in client.get_library_playlists(limit=None)] == ["ONE", "TWO"]
    assert len(bridge.calls) == 2


class Ticks:
    def __init__(self, count):
        self.remaining = count

    def wait(self, timeout):
        self.remaining -= 1
        return self.remaining < 0


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("failure", ["provider", "sync"])
def test_manual_and_automatic_failures_publish_error_before_terminal_and_retry(
        store, monkeypatch, automatic, failure):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    calls = []
    provider_failed = False

    def provider():
        nonlocal provider_failed
        if failure == "provider" and not provider_failed:
            provider_failed = True
            raise RuntimeError("provider unavailable")
        return {iid: Client()}

    real_sync = sync.sync_all

    def execute(*args, **kwargs):
        calls.append(True)
        if failure == "sync" and len(calls) == 1:
            kwargs["on_progress"]({"type": "step", "text": "Started library"})
            raise RuntimeError("sync interrupted")
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(sync, "sync_all", execute)
    monkeypatch.setattr("yt_playlist.web.app._maybe_check_update", lambda ctx: None)
    app = create_app(store, provider, now_fn=lambda: 200, background_work=False)
    ctx = app.state.ctx
    ctx.rec_worker = ctx.enrich_worker = None
    browser = TestClient(app, base_url="http://127.0.0.1")
    if automatic:
        automatic_sync(ctx, None, SimpleNamespace(connected=True), stop=Ticks(1))
        jid = 1
    else:
        jid = browser.post("/sync").json()["job_id"]
    events = [json.loads(line[6:]) for line in browser.get(f"/sync/events/{jid}").text.splitlines()
              if line.startswith("data: ")]
    assert any(event["type"] == "err" for event in events[:-1])
    terminal = events[-1]
    assert terminal["outcome"] == "failed"
    assert terminal["error"]
    assert terminal["result"]["status"] == "failed"
    assert store.settings.get_setting("last_sync_at") is None
    assert ctx.sync_lock.acquire(blocking=False)
    ctx.sync_lock.release()
    assert ctx.work.wait(1)
    # A second automatic tick must survive the exception and complete the recovered run.
    automatic_sync(ctx, None, SimpleNamespace(connected=True), stop=Ticks(1))
    assert ctx.jobs.get(jid + 1).outcome == "complete"
    assert store.settings.get_setting("last_sync_at") == "200"


def test_automatic_loop_retries_partial_on_next_tick(store, monkeypatch):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = Client()
    client.failed_resource = "history"
    calls = []

    def provider():
        if calls:
            client.failed_resource = None
        calls.append(True)
        return {iid: client}

    monkeypatch.setattr("yt_playlist.web.app._maybe_check_update", lambda ctx: None)
    ctx = SimpleNamespace(store=store, work=Activity(), jobs=SyncJobs(), sync_lock=threading.Lock(),
        client_provider=provider, now_fn=lambda: 200, rec_worker=None, enrich_worker=None,
        flag_auth_expired=None, clear_auth_expired=None, logger=logging.getLogger(__name__))
    automatic_sync(ctx, None, SimpleNamespace(connected=True), stop=Ticks(3))
    assert len(calls) == 2
    assert ctx.jobs.get(1).outcome == "partial"
    assert ctx.jobs.get(2).outcome == "complete"
    assert not ctx.jobs.has_active()
    assert ctx.work.wait(0)
