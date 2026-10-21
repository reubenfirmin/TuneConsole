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
