"""Installed MV3 worker, real worlds/tabs/WebSocket, and persisted backend attribution.

No Google account or external network is needed. Music's private APIs and responses are
fixtures; extension APIs, world isolation, media, and backend ingestion run for real.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
import wave

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
import pytest
import uvicorn

from yt_playlist.core.bridge import Bridge
from yt_playlist.web.routes.bridge import build


EXT = Path(__file__).resolve().parents[2] / "extension"
HTML = Path(__file__).with_name("fixtures") / "music_player.html"


@pytest.fixture
def installed_bridge(playwright, browser_type_launch_args, tmp_path, store, request):
    if shutil.which("openssl") is None:
        pytest.fail("OpenSSL is required for the local Music TLS fixture")
    bridge = Bridge()
    main = store.identities.upsert_identity("main", "bridge", None, True)
    brand = store.identities.upsert_identity("band", "bridge", "brand-A", False)
    ctx = SimpleNamespace(bridge=bridge, store=store, now_fn=time.time)
    app = FastAPI()
    app.include_router(build(ctx))
    # Bind before starting the server so another test/process cannot take the chosen port.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "fixture bridge failed to start"

    # Change only the local test endpoint/permission in a copy; the source manifest and
    # production endpoint remain untouched. Retain the real unpacked extension identity.
    extension = tmp_path / "extension"
    shutil.copytree(EXT, extension)
    config = extension / "worker" / "config.js"
    config.write_text(config.read_text().replace(":8765", f":{port}"))
    bootstrap = getattr(request, "param", "current")
    if bootstrap != "current":
        background = extension / "background.js"
        source = background.read_text().replace("receiverRevision: TC_CONFIG.receiverRevision, ", "")
        if bootstrap == "legacy_bootstrap":
            source = source.replace("revision: TC_CONFIG.sensorRevision, ", "")
        background.write_text(source)
    manifest = extension / "manifest.json"
    data = json.loads(manifest.read_text())
    data["host_permissions"] = [s.replace(":8765", f":{port}") for s in data["host_permissions"]]
    manifest.write_text(json.dumps(data))
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 8000 * 30)
    requests = []
    # An extension-created tab can start its first navigation before Playwright attaches
    # request interception. A local TLS origin also covers that initial browser request.
    key, certificate = tmp_path / "music.key", tmp_path / "music.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(certificate), "-days", "1",
                    "-subj", "/CN=music.youtube.com"],
                   check=True, capture_output=True, timeout=10)

    @app.get("/fixture.wav")
    def fixture_audio():
        return Response(audio.getvalue(), media_type="audio/wav")

    @app.post("/youtubei/v1/{endpoint:path}")
    async def fixture_api(endpoint: str, request: Request):
        requests.append({"url": str(request.url), "body": await request.json()})
        return {"fixture": True}

    @app.get("/{path:path}")
    def fixture_page(path: str):
        return HTMLResponse(HTML.read_text())

    tls_listener = socket.socket()
    tls_listener.bind(("127.0.0.1", 0))
    tls_port = tls_listener.getsockname()[1]
    tls_server = uvicorn.Server(uvicorn.Config(app, log_level="warning",
                                              ssl_keyfile=str(key), ssl_certfile=str(certificate)))
    tls_thread = threading.Thread(target=lambda: tls_server.run(sockets=[tls_listener]), daemon=True)
    tls_thread.start()
    deadline = time.monotonic() + 5
    while not tls_server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tls_server.started, "fixture Music origin failed to start"
    context = None
    try:
        context = playwright.chromium.launch_persistent_context(
            tmp_path / "profile", **{**browser_type_launch_args, "channel": "chromium"},
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}",
                  "--autoplay-policy=no-user-gesture-required", "--ignore-certificate-errors",
                  "--no-proxy-server", f"--host-resolver-rules=MAP music.youtube.com 127.0.0.1:{tls_port}, "
                  "MAP lh3.googleusercontent.com ~NOTFOUND"],
        )
        context.route("https://lh3.googleusercontent.com/**", lambda route: route.fulfill(status=204))
        context.add_cookies([{"name": "SAPISID", "value": "fixture-cookie",
                             "url": "https://music.youtube.com"}])
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        harness = SimpleNamespace(context=context, worker=worker, bridge=bridge, store=store,
                                  main=main, brand=brand, requests=requests, extension=extension)

        def drop_connection():
            def close():
                for connection in tuple(server.server_state.connections):
                    connection.transport.close()
            bridge._loop.call_soon_threadsafe(close)

        harness.drop_connection = drop_connection

        def eventually(predicate, message="extension condition timed out", timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                # Pump Playwright's event loop so routed fetches and page messages progress.
                context.pages[0].wait_for_timeout(25)
            assert predicate(), message() if callable(message) else message

        harness.eventually = eventually
        eventually(lambda: bridge.connected, "installed extension did not connect")
        yield harness
    finally:
        if context is not None:
            context.close()
        server.should_exit = True
        tls_server.should_exit = True
        thread.join(timeout=5)
        tls_thread.join(timeout=5)
        listener.close()
        tls_listener.close()


def _music(h, query="?playing=true"):
    page = h.context.new_page()
    page.goto("https://music.youtube.com/" + query)
    page.wait_for_function("window.fixture && window.__tcNowPoller")
    if "playing=true" in query:
        page.wait_for_function("!document.querySelector('video').paused")
    return page


def _sample(h, page):
    page.evaluate("fixture.sample()")
    # MAIN replies to a real isolated-world health request, even when intervals are stopped.
    tab_id = h.worker.evaluate("""async url => (await chrome.tabs.query({})).find(t => t.url === url).id""", page.url)
    assert h.worker.evaluate("id => tuneConsole.sensors.ping(id)", tab_id)["ok"]


def _plays(h):
    return h.store.history.play_events_since(0)


def test_worlds_delayed_metadata_and_ratings_keep_account_and_video_identity(installed_bridge):
    h = installed_bridge
    page = _music(h, "?playing=true&known=false")
    _sample(h, page)
    h.eventually(lambda: h.bridge.now_playing is not None)
    assert _plays(h) == [], "unknown account must not persist as the main account"
    page.evaluate("fixture.known = true; fixture.sample()")
    h.eventually(lambda: len(_plays(h)) == 1)
    assert _plays(h)[0]["identity_id"] == h.brand
    assert _plays(h)[0]["video_id"] == "first"
    assert h.bridge.now_playing["thumbnail"] == "https://lh3.googleusercontent.com/album-art"
    page.evaluate("fixture.setRating('DISLIKE'); fixture.sample()")
    h.eventually(lambda: h.store.tracks.item_rating(h.brand, "first") is not None)
    assert h.store.tracks.item_rating(h.brand, "first")["status"] == "DISLIKE"

    # Player changes before MediaSession, the player bar, and the rating target catch up.
    page.evaluate("fixture.videoId = 'second'; fixture.title = 'Second song'; fixture.sample()")
    h.eventually(lambda: h.bridge.now_playing.get("video_id") == "second")
    assert len(_plays(h)) == 1
    page.evaluate("fixture.setBar(); fixture.setMedia(); fixture.sample()")
    h.eventually(lambda: len(_plays(h)) == 2)
    assert h.store.tracks.item_rating(h.brand, "second") is None
    page.evaluate("fixture.setRating('LIKE'); fixture.sample()")
    h.eventually(lambda: h.store.tracks.item_rating(h.brand, "second") is not None)
    assert h.store.tracks.item_rating(h.brand, "second")["status"] == "LIKE"
    assert h.store.tracks.item_rating(h.brand, "first")["status"] == "DISLIKE"
    assert h.store.tracks.item_rating(h.main, "second") is None
    assert [(p["video_id"], p["identity_id"]) for p in _plays(h)] == [
        ("first", h.brand), ("second", h.brand)]


@pytest.mark.parametrize("settlement", ["binding_ready", "song_changed", "account_changed", "newer_rating"])
def test_native_rating_waits_for_bound_controls_and_cancels_old_intent(installed_bridge, settlement):
    h = installed_bridge
    page = _music(h)
    _sample(h, page)
    h.worker.evaluate("""() => {
        globalThis.ratingFrames = [];
        const send = tuneConsole.transport.send;
        tuneConsole.transport.send = frame => {
            if (frame.type === 'rating-reconciliation') ratingFrames.push(frame);
            return send(frame);
        };
    }""")
    page.evaluate("""() => {
        document.querySelector('ytmusic-player-bar').data = undefined;
        document.querySelector('ytmusic-like-button-renderer').data = undefined;
        fixture.sample();
    }""")

    def rate(desired):
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(asyncio.run, h.bridge.request_control({
                "type": "rate", "action": "like", "videoId": "first",
                "likeStatus": desired, "brandId": "brand-A"}))
            h.eventually(result.done)
            return json.loads(result.result()[1])

    assert rate("LIKE") == {"ok": True, "pending": True}
    assert page.evaluate("fixture.clicks") == []
    first_id = h.worker.evaluate("ratingFrames[0].ratingId")
    if settlement == "newer_rating":
        assert rate("INDIFFERENT")["pending"]
    page.evaluate("""settlement => {
        if (settlement === 'song_changed') fixture.videoId = 'second';
        if (settlement === 'account_changed') fixture.brand = 'brand-B';
        fixture.setBar(); fixture.setRating(); fixture.sample();
    }""", settlement)
    h.eventually(lambda: h.worker.evaluate("ratingFrames.at(-1)?.state") in ("recovered", "cancelled"))
    frames = h.worker.evaluate("ratingFrames")
    first = [frame for frame in frames if frame["ratingId"] == first_id]
    assert [frame["state"] for frame in first] == [
        "pending", "recovered" if settlement == "binding_ready" else "cancelled"]
    assert first[0]["first"]["diagnostics"]["binding"]["targetIds"] == []
    if settlement == "binding_ready":
        assert rate("LIKE")["ok"]  # Reapplying a confirmed desired state sends no second click.
    assert page.evaluate("fixture.clicks") == (["like"] if settlement == "binding_ready" else [])


@pytest.mark.parametrize("installed_bridge", ["legacy_bootstrap", "missing_receiver_option"], indirect=True)
def test_legacy_bootstrap_reconnects_without_reloading_music(installed_bridge):
    h = installed_bridge
    page = _music(h)
    page.evaluate("window.documentToken = 'same document'")
    h.eventually(lambda: len(_plays(h)) == 1)
    h.eventually(lambda: (h.bridge.sensor_health or {}).get("healthy"))

    connection_id = h.bridge._conn_id
    h.drop_connection()
    h.eventually(lambda: h.bridge.connected and h.bridge._conn_id != connection_id)
    h.eventually(lambda: (h.bridge.sensor_health or {}).get("healthy"))
    h.eventually(lambda: (h.bridge.now_playing or {}).get("video_id") == "first")
    assert page.evaluate("documentToken") == "same document"
    assert page.evaluate("!document.querySelector('video').paused")
    assert len(_plays(h)) == 1
    assert h.worker.evaluate("tuneConsole.sensors.probe()")["reloadedTabs"] == 0


def test_extension_reload_reconnect_and_throttled_main_repair_preserve_playback(installed_bridge):
    h = installed_bridge
    page = _music(h)
    h.eventually(lambda: len(_plays(h)) == 1)
    page.evaluate("window.documentToken = 'same document'; window.__tcNowPoller.stop()")
    # Health intentionally tolerates ten seconds without a sample. Let that real freshness
    # window expire before proving stale-sensor repair (without changing production clocks).
    page.wait_for_timeout(10100)
    # A stopped poller is replaced in MAIN without reloading a playing document.
    report = h.worker.evaluate("tuneConsole.sensors.probe()")
    assert report["healthy"]
    assert report["reinjectedTabs"] == 1
    assert report["reloadedTabs"] == 0
    assert page.evaluate("documentToken") == "same document"

    # Stop interval ticks only: the explicit MAIN handshake must still sample immediately.
    page.evaluate("""() => {
        const interval = window.setInterval;
        window.setInterval = () => 0;
        window.__tcNowPoller.stop();
        window.__tcNowPoller = null;
        window.originalInterval = interval;
    }""")
    h.worker.evaluate("tuneConsole.sensors.injectAll()")
    assert h.worker.evaluate("tuneConsole.sensors.probe()")["healthy"]
    page.evaluate("fixture.title = 'While throttled'; fixture.setBar(); fixture.setMedia()")
    _sample(h, page)
    h.eventually(lambda: h.bridge.now_playing.get('title') == 'While throttled')
    assert page.evaluate("!document.querySelector('video').paused")

    connection_id = h.bridge._conn_id
    h.drop_connection()
    h.eventually(lambda: h.bridge.connected and h.bridge._conn_id != connection_id,
                 "worker did not reconnect after the real socket dropped")
    h.eventually(lambda: (h.bridge.now_playing or {}).get("video_id") == "first",
                 "reconnected worker did not restore the playing track")
    assert len(_plays(h)) == 1

    # Actual MV3 reload destroys the worker and invalidates isolated contexts. Its install
    # lifecycle installs fresh sensors into the still-playing, existing Music document.
    manager = h.context.new_page()
    manager.goto("chrome://extensions/")
    manager.locator("#devMode").click()
    manifest_path = h.extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    version = manifest["version"].split(".")
    version[-1] = str(int(version[-1]) + 1)
    manifest["version"] = ".".join(version)
    manifest_path.write_text(json.dumps(manifest))
    with h.context.expect_event("serviceworker") as replacement:
        manager.locator("extensions-item").filter(has_text="TuneConsole").locator("#dev-reload-button").click()
    h.worker = replacement.value
    manager.close()
    h.eventually(lambda: h.worker.evaluate("tuneConsole.transport.connected"))
    h.eventually(lambda: h.worker.evaluate("tuneConsole.sensors.probe()")["healthy"])
    assert page.evaluate("documentToken") == "same document"
    assert page.evaluate("!document.querySelector('video').paused")
    # Sensor health can precede backend ingestion. Wait past the DOM fallback,
    # whose album artwork does not identify the playing video.
    h.eventually(lambda: (h.bridge.now_playing or {}).get("video_id") == "first",
                 "reloaded extension did not restore the playing track")
    assert len(_plays(h)) == 1, "reconnect must not duplicate persisted plays"
    assert h.bridge.now_playing["video_id"] == "first"


def test_multiple_tabs_control_routing_navigation_and_removal(installed_bridge):
    h = installed_bridge
    owner = _music(h, "?playing=true&v=owner")
    h.eventually(lambda: len(_plays(h)) == 1)
    browsing = _music(h, "?v=browsing")
    _sample(h, browsing)
    # Native Chrome audible flags can lag; send ordered samples before selecting controls.
    _sample(h, owner)
    h.worker.evaluate("tuneConsole.playback.drained")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(asyncio.run, h.bridge.request_control({
            "type": "rate", "action": "like", "videoId": "owner", "likeStatus": "LIKE"}))
        h.eventually(result.done, "backend rating control did not round-trip")
        assert json.loads(result.result()[1])["ok"]
    h.eventually(lambda: h.store.tracks.item_rating(h.brand, "owner") is not None)
    assert owner.evaluate("fixture.clicks") == ["like"]
    assert browsing.evaluate("fixture.clicks") == []
    assert h.store.tracks.item_rating(h.brand, "browsing") is None

    h.bridge.send_control({"type": "playpause"})
    owner.wait_for_function("document.querySelector('video').paused")
    assert browsing.evaluate("fixture.clicks") == []
    owner.goto("https://music.youtube.com/?playing=true&v=after_navigation")
    owner.wait_for_function("window.__tcNowPoller && !document.querySelector('video').paused")
    _sample(h, owner)
    h.eventually(lambda: any(p["video_id"] == "after_navigation" for p in _plays(h)))
    owner.close()
    h.eventually(lambda: h.worker.evaluate("tuneConsole.playback.pickTab().then(t => t?.url)") == browsing.url)
    h.bridge.send_control({"type": "playpause"})
    browsing.wait_for_function("!document.querySelector('video').paused")
    h.eventually(lambda: any(p["video_id"] == "browsing" for p in _plays(h)))
    assert browsing.evaluate("fixture.clicks") == ["playpause"]


def test_dislike_updates_native_thumb_then_skips_the_radio_queue(installed_bridge):
    h = installed_bridge
    owner = _music(h, "?playing=true&v=radio-song&list=PL_radio")
    h.eventually(lambda: len(_plays(h)) == 1)
    _sample(h, owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(asyncio.run, h.bridge.request_control({
            "type": "rate", "action": "dislike", "videoId": "radio-song",
            "likeStatus": "DISLIKE", "brandId": "brand-A", "skip": True}))
        h.eventually(result.done, "dislike-and-skip did not round-trip")
        assert json.loads(result.result()[1])["ok"]
    h.eventually(lambda: h.bridge.now_playing.get("video_id") == "radio-song-next")
    assert owner.evaluate("fixture.clicks") == ["dislike", "next"]
    h.eventually(lambda: h.store.tracks.item_rating(h.brand, "radio-song") is not None)
    assert h.store.tracks.item_rating(h.brand, "radio-song")["status"] == "DISLIKE"
    assert h.store.tracks.item_rating(h.brand, "radio-song-next") is None
    assert owner.evaluate("fixture.playlist") == "PL_radio"
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(asyncio.run, h.bridge.request_control({
            "type": "next", "videoId": "radio-song"}))
        h.eventually(result.done)
        assert not json.loads(result.result()[1])["ok"]
    assert owner.evaluate("fixture.clicks") == ["dislike", "next"]


@pytest.mark.parametrize("first", ["launch", "fetch"])
def test_library_fetch_and_launch_share_one_real_tab_and_deliver_each_request(installed_bridge, first):
    h = installed_bridge
    target = "https://music.youtube.com/?playing=true&v=launched&list=PL_launch"
    with ThreadPoolExecutor(max_workers=2) as pool:
        def fetch(index):
            return h.bridge.execute("POST", "https://music.youtube.com/youtubei/v1/browse",
                                    {"request": index}, timeout=8)

        if first == "launch":
            h.bridge.send_control({"type": "navigate", "url": target})
        futures = [pool.submit(fetch, index) for index in (1, 2)]
        if first == "fetch":
            h.bridge.send_control({"type": "navigate", "url": target})
        h.eventually(lambda: all(f.done() for f in futures), "library requests did not finish")
        assert [f.result()[0] for f in futures] == [200, 200]
    tabs = h.worker.evaluate("tuneConsole.tabs.list()")
    assert len(tabs) == 1
    assert tabs[0]["url"] == target
    assert sorted(r["body"]["request"] for r in h.requests) == [1, 2]
    diagnostics = [(p.url, p.evaluate("window.fixture && ({id:fixture.videoId, "
                                    "paused:document.querySelector('video').paused})"))
                   for p in h.context.pages]
    h.eventually(lambda: len(_plays(h)) == 1, lambda:
                 f"launch play missing: plays={_plays(h)}, now={h.bridge.now_playing}, "
                 f"health={h.bridge.sensor_health}, tabs={tabs}, pages={diagnostics}")
    assert _plays(h)[0]["video_id"] == "launched"
    assert _plays(h)[0]["playlist_ytm_id"] == "PL_launch"
    assert _plays(h)[0]["identity_id"] == h.brand
