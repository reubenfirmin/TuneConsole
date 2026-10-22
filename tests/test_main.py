import pytest
from yt_playlist.__main__ import sync_identities_into_store, validate_credentials
from yt_playlist.core.config import IdentityConfig

def test_sync_identities_into_store_inserts_once(store):
    cfgs = [IdentityConfig("main", "cred.json", None, True),
            IdentityConfig("brand", "cred.json", "999", False)]
    ids = sync_identities_into_store(store, cfgs)
    assert len(ids) == 2
    # idempotent: calling again does not duplicate
    ids2 = sync_identities_into_store(store, cfgs)
    assert len(store.identities.get_identities()) == 2
    assert ids == ids2

def test_validate_credentials_missing_raises(tmp_path):
    cfgs = [IdentityConfig("main", "cred.json", None, True)]
    with pytest.raises(SystemExit) as exc_info:
        validate_credentials(cfgs, base_dir=tmp_path)
    assert "cred.json" in str(exc_info.value)
    assert "main" in str(exc_info.value)

def test_validate_credentials_present_passes(tmp_path):
    cred_file = tmp_path / "cred.json"
    cred_file.write_text("{}")
    cfgs = [IdentityConfig("main", "cred.json", None, True)]
    # Should not raise
    validate_credentials(cfgs, base_dir=tmp_path)


def test_restart_waits_until_old_listener_closes(monkeypatch):
    from yt_playlist import __main__ as main
    from contextlib import nullcontext
    attempts = []
    def connect(address, timeout):
        attempts.append(address)
        if len(attempts) == 3:
            raise ConnectionRefusedError()
        return nullcontext()
    monkeypatch.setattr(main.socket, "create_connection", connect)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    main._wait_for_port("127.0.0.1", 9123)
    assert attempts == [("127.0.0.1", 9123)] * 3


def test_restart_fails_without_starting_when_old_listener_wont_close(monkeypatch):
    from yt_playlist import __main__ as main
    monkeypatch.setattr(main.time, "monotonic", iter([0, 31]).__next__)
    with pytest.raises(SystemExit, match="previous TuneConsole process did not stop"):
        main._wait_for_port("127.0.0.1", 9123)


def test_native_host_can_run_and_stop_http_server_on_a_worker_thread(monkeypatch):
    import http.client
    import queue
    import threading
    import time

    from fastapi import FastAPI
    from yt_playlist import __main__ as main

    app = FastAPI()
    updater = object()
    attached = queue.Queue()
    failures = []

    @app.get("/health")
    def health():
        return {"ok": True}

    def build_app(**kwargs):
        assert kwargs["updater"] is updater
        return app

    monkeypatch.setattr(main, "build_app", build_app)
    monkeypatch.setattr(main.logsetup, "configure", lambda: None)

    def serve():
        try:
            main.main(["--port", "0"], updater=updater, server_callback=attached.put)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    server = attached.get(timeout=5)
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, failures
        port = server.servers[0].sockets[0].getsockname()[1]
        # This is an external test client. urllib is process-wide guarded by the
        # app (including guards installed by earlier tests), which blocks localhost.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b'{"ok":true}'
        finally:
            connection.close()
    finally:
        server.should_exit = True
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert not failures


def test_quit_stops_disconnected_sync_and_closes_its_live_stream(store, monkeypatch):
    import http.client
    import json
    import queue
    import signal
    import threading
    import time

    from tests.conftest import FakeClient
    from yt_playlist import __main__ as main
    from yt_playlist.core.bridge import BridgeError
    from yt_playlist.web.app import create_app

    entered, release = threading.Event(), threading.Event()

    class Disconnected(FakeClient):
        reads = []

        def get_playlist(self, playlistId, limit=None):
            self.reads.append(playlistId)
            entered.set()
            assert release.wait(5)
            raise BridgeError("no extension connected")

    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    tid = store.tracks.upsert_track("kept", "Cached song", "Artist", "", 180)
    cached = store.playlists.upsert_playlist(iid, "OLD", "Cached playlist", 1, "cached", 1)
    store.playlists.set_playlist_tracks(cached, [tid])
    store.settings.set_setting("last_sync_at", "1")
    remote = Disconnected(playlists=[{"playlistId": f"NEXT-{n}", "title": f"Playlist {n}"}
                                     for n in range(100)])
    app = create_app(store, lambda: {iid: remote}, now_fn=lambda: 1000, background_work=False)
    monkeypatch.setattr(main, "build_app", lambda **kwargs: app)
    monkeypatch.setattr(main.logsetup, "configure", lambda: None)
    attached, failures = queue.Queue(), []

    def serve():
        try:
            main.main(["--port", "0"], server_callback=attached.put)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    server = attached.get(timeout=5)
    connection = stream = None
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, failures
        port = server.servers[0].sockets[0].getsockname()[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", "/sync")
        response = connection.getresponse()
        assert response.status == 200
        job_id = json.loads(response.read())["job_id"]
        assert entered.wait(5)
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        stream.request("GET", f"/sync/events/{job_id}")
        events = stream.getresponse()
        assert events.status == 200

        # Exercise the same handler as the first Ctrl-C while the bridge read is in flight.
        server.handle_exit(signal.SIGINT, None)
        release.set()
        assert b'"type": "end"' in events.read()
        worker.join(timeout=5)
        assert not worker.is_alive(), app.state.ctx.work.active
        assert not failures
        assert remote.reads == ["NEXT-0"]
        assert app.state.ctx.work.wait(0)
        assert app.state.ctx.jobs.get(job_id).done
        assert store.settings.get_setting("last_sync_at") == "1"
        assert [p.ytm_playlist_id for p in store.playlists.get_playlists()] == ["OLD"]
        assert store.playlists.get_playlist_track_keys(cached) == {"cached song|artist"}
    finally:
        release.set()
        server.should_exit = True
        if connection is not None:
            connection.close()
        if stream is not None:
            stream.close()
        worker.join(timeout=5)
