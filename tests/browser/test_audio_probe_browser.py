"""Real isolated-world element capture, with Chromium's fake hardware output."""
import io
import json
import math
from pathlib import Path
import shutil
import socket
import struct
import wave

import pytest
from playwright.sync_api import expect


EXT = Path(__file__).resolve().parents[2] / "extension"


def _wav(*, silent=False):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(struct.pack("<h", 0 if silent else int(
            3276 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000 * 12)))
    return buffer.getvalue()


@pytest.fixture
def probe_browser(playwright, browser_type_launch_args, tmp_path, request):
    extension = tmp_path / "extension"
    shutil.copytree(EXT, extension)
    # The access-only diagnostic is explicitly installed in this test profile;
    # ordinary extension startup must not capture audio outside the app's flag.
    manifest_path = extension / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for content in manifest['content_scripts']:
        if content.get('js', [])[:1] == ['content.js']:
            content['js'].append('audio_probe.js')
    manifest_path.write_text(json.dumps(manifest))
    # Do not connect this test extension to the user's running TuneConsole app.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    config = extension / "worker" / "config.js"
    config.write_text(config.read_text().replace(":8765", f":{port}"))
    context = playwright.chromium.launch_persistent_context(
        tmp_path / "profile", **{**browser_type_launch_args, "channel": "chromium"},
        ignore_default_args=["--mute-audio"],
        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}",
              # FakeAudioManager processes PCM but never opens speaker hardware.
              "--disable-audio-output"],
    )
    sound, silence = _wav(), _wav(silent=True)

    def music(route):
        if route.request.url.endswith(".wav"):
            route.fulfill(content_type="audio/wav", body=(
                silence if route.request.url.endswith("silent.wav") else sound))
        else:
            route.fulfill(content_type="text/html", headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'none'; "
                "media-src 'self' blob: https://audio.tc-probe.invalid",
            }, body='<title>Audio access fixture</title><video src="/probe.wav" loop></video>')

    context.route("https://music.youtube.com/**", music)
    context.route("https://audio.tc-probe.invalid/**", lambda route: route.fulfill(
        content_type="audio/wav", body=sound))
    context.route("https://tuneconsole.com/**", lambda route: route.fulfill(
        content_type="text/html", body="<h1>Setup fixture</h1>"))
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        page = context.new_page()
        page.goto("https://music.youtube.com/")
        if getattr(request, "param", None) == "no_activation":
            # Even Playwright's usual DOM assertions/injected evaluations can
            # carry debugger user activation. This variant uses raw CDP only
            # until capture and decoding finish, explicitly disabling gestures.
            yield context, worker, page, None
            return
        expect(page.locator("#tuneconsole-audio-probe")).to_be_visible()
        tabs = worker.evaluate("chrome.tabs.query({url:'https://music.youtube.com/*'})")
        assert len(tabs) == 1
        tab_id = tabs[0]["id"]
        # Instrument the actual content-script world, not the page world. The
        # stream and context remain native browser objects throughout these tests.
        worker.evaluate("""tabId => chrome.scripting.executeScript({target:{tabId}, func:() => {
          window.probeStreams = []; window.probeContexts = [];
          window.probeRecorders = []; window.probePCM = [];
          const capture = HTMLMediaElement.prototype.captureStream;
          HTMLMediaElement.prototype.captureStream = function() {
            const stream = capture.call(this); probeStreams.push(stream); return stream;
          };
          const NativeContext = AudioContext;
          window.AudioContext = class extends NativeContext {
            constructor(...args) { super(...args); probeContexts.push(this); }
          };
          const NativeRecorder = MediaRecorder, NativeOffline = OfflineAudioContext;
          window.MediaRecorder = class extends NativeRecorder {
            constructor(...args) { super(...args); probeRecorders.push(this); }
          };
          window.OfflineAudioContext = class extends NativeOffline {
            async decodeAudioData(...args) {
              const pcm = await super.decodeAudioData(...args); probePCM.push(pcm); return pcm;
            }
          };
          navigator.mediaDevices.getDisplayMedia = () => {throw Error('No sharing chooser allowed');};
          navigator.mediaDevices.getUserMedia = () => {throw Error('No microphone allowed');};
        }})""", tab_id)
        yield context, worker, page, tab_id
    finally:
        context.close()


def _resources(worker, tab_id):
    result = worker.evaluate("""tabId => chrome.scripting.executeScript({target:{tabId}, func:() => ({
      streams: probeStreams.length, contexts: probeContexts.length,
      tracksEnded: probeStreams.every(stream => stream.getTracks().every(track => track.readyState === 'ended')),
      contextsClosed: probeContexts.every(context => context.state === 'closed'),
      recordersInactive: probeRecorders.every(recorder => recorder.state === 'inactive'),
      pcmErased: probePCM.every(pcm => Array.from({length:pcm.numberOfChannels}, (_, channel) =>
        pcm.getChannelData(channel).every(sample => sample === 0)).every(Boolean)),
    })})""", tab_id)
    return result[0]["result"]


def _report(page):
    return json.loads(page.locator("#tuneconsole-audio-probe pre").text_content())


def _released(worker, tab_id):
    state = _resources(worker, tab_id)
    assert state["tracksEnded"] and state["contextsClosed"] and state["recordersInactive"] and state["pcmErased"], state


@pytest.mark.parametrize("source_kind", ["http", "blob"])
def test_direct_capture_reads_real_pcm_without_chooser_and_keeps_playback(probe_browser, tmp_path, source_kind):
    context, worker, page, tab_id = probe_browser
    banner = page.locator("#tuneconsole-audio-probe")
    page.set_viewport_size({"width": 390, "height": 900})
    if source_kind == "blob":
        page.locator("video").evaluate("""async media => {
          const response = await fetch('/probe.wav');
          media.src = URL.createObjectURL(await response.blob()); await media.play();
        }""")
    else:
        page.locator("video").evaluate("media => media.play()")
    expect(banner.get_by_role("status")).to_contain_text("Direct audio access works", timeout=10000)
    report = _report(page)
    assert report["outcome"] == "available"
    assert report["drm_media_keys"] is False
    assert report["source_kind"] == source_kind
    assert report["rms"] == pytest.approx(.1 / 2**.5, abs=.015)
    assert report["peak"] > .08 and report["recording_chunks"] >= 10
    assert report["trigger"] == "automatic" and report["realtime_audio_context_used"] is False
    assert report["recording_mime"] == "audio/webm;codecs=pcm" and report["decoded_seconds"] > 2.5
    assert report["measured_seconds"] >= 3
    assert report["audio_tracks"] == 1 and report["playback_continued"] is True
    assert report["media_time_advanced"] >= 2.5
    assert report["raw_audio_retained"] is False and report["source_identity_verified"] is False
    assert "probe.wav" not in json.dumps(report)
    _released(worker, tab_id)
    assert page.locator("video").evaluate("media => !media.paused")
    # Native captureStream can add tracks after Stop when the element loads a
    # different source. They must be ended too, with no new analysis context.
    page.locator("video").evaluate("async media => {media.src='/silent.wav'; await media.play();}")
    _released(worker, tab_id)
    assert _resources(worker, tab_id)["contexts"] == 0
    assert banner.evaluate("host => host.getBoundingClientRect().left >= 0 && "
                           "host.getBoundingClientRect().right <= innerWidth")
    page.screenshot(path=str(tmp_path / "audio-access-mobile.png"), full_page=True)
    # Native EME state does not have to leak through a page-world JS bridge.
    assert page.evaluate("window.__tcAudioProbe === undefined")


def test_real_media_keys_block_capture_before_creating_any_stream(probe_browser):
    context, worker, page, tab_id = probe_browser
    page.locator("video").evaluate("""async media => {
      media.pause(); media.removeAttribute('src'); media.load();
      const access = await navigator.requestMediaKeySystemAccess('org.w3.clearkey', [{
        initDataTypes:['cenc'], audioCapabilities:[{contentType:'audio/mp4; codecs="mp4a.40.2"'}],
      }]);
      await media.setMediaKeys(await access.createMediaKeys());
      media.src = '/probe.wav'; await media.play();
    }""")
    banner = page.locator("#tuneconsole-audio-probe")
    expect(banner.get_by_role("status")).to_have_text("This player uses DRM. Direct audio capture is blocked.")
    assert _report(page)["outcome"] == "drm_blocked"
    assert _report(page)["drm_media_keys"] is True
    assert _resources(worker, tab_id)["streams"] == _resources(worker, tab_id)["contexts"] == 0
    assert page.locator("video").evaluate("media => !media.paused")


def test_cross_origin_media_is_distinguished_from_drm(probe_browser):
    context, worker, page, tab_id = probe_browser
    page.locator("video").evaluate("""async media => {
      media.src = 'https://audio.tc-probe.invalid/probe.wav'; await media.play();
    }""")
    banner = page.locator("#tuneconsole-audio-probe")
    expect(banner.get_by_role("status")).to_contain_text("cross-origin restrictions")
    assert _report(page)["outcome"] == "cross_origin_blocked"
    assert _report(page)["drm_media_keys"] is False
    _released(worker, tab_id)
    assert page.locator("video").evaluate("media => !media.paused")


def test_silence_is_inconclusive_and_does_not_claim_drm(probe_browser):
    context, worker, page, tab_id = probe_browser
    page.locator("video").evaluate("async media => {media.src='/silent.wav'; await media.play();}")
    banner = page.locator("#tuneconsole-audio-probe")
    expect(banner.get_by_role("status")).to_contain_text("No usable signal", timeout=10000)
    assert _report(page)["outcome"] == "no_signal"
    assert _report(page)["rms"] == 0 and _report(page)["rms_dbfs"] is None
    _released(worker, tab_id)


@pytest.mark.parametrize("end", ["stop", "dismiss", "pause", "source", "navigate"])
def test_probe_cancellation_releases_its_tracks_and_context(probe_browser, end):
    context, worker, page, tab_id = probe_browser
    banner = page.locator("#tuneconsole-audio-probe")
    page.locator("video").evaluate("media => media.play()")
    expect(banner.get_by_role("button", name="Stop check", exact=True)).to_be_enabled()
    assert _resources(worker, tab_id)["streams"] == 1
    if end == "stop":
        banner.get_by_role("button", name="Stop check", exact=True).click()
    elif end == "dismiss":
        banner.get_by_role("button", name="Dismiss audio access check").click()
        expect(banner).to_have_count(0)
    elif end == "pause":
        page.locator("video").evaluate("media => media.pause()")
    elif end == "source":
        page.locator("video").evaluate("async media => {media.src='/silent.wav'; await media.play();}")
    else:
        # pagehide tears down before navigation; inspect the old native resources
        # while the document still exists, then exercise an actual page reload.
        page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide'))")
        expect(banner).to_have_count(0)
    _released(worker, tab_id)
    if end != "pause":
        assert page.locator("video").evaluate("media => !media.paused")
    if end == "dismiss":
        page.reload()
        expect(banner).to_have_count(0)
    elif end == "navigate":
        page.reload()
        expect(banner).to_be_visible()
    else:
        assert _report(page)["outcome"] in {"stopped", "playback_changed", "capture_ended"}


def test_injection_is_idempotent_and_paused_player_does_not_start_capture(probe_browser):
    context, worker, page, tab_id = probe_browser
    worker.evaluate("tabId => chrome.scripting.executeScript({target:{tabId}, files:['audio_probe.js']})", tab_id)
    banner = page.locator("#tuneconsole-audio-probe")
    expect(banner).to_have_count(1)
    page.locator("video").evaluate("media => media.pause()")
    banner.get_by_role("button", name="Run check again", exact=True).click()
    expect(banner.get_by_role("status")).to_contain_text("Play a song")
    assert _report(page)["outcome"] == "no_playing_media"
    assert _resources(worker, tab_id)["streams"] == _resources(worker, tab_id)["contexts"] == 0


def test_worker_startup_does_not_restart_access_probe_outside_flagged_app(probe_browser):
    context, worker, page, tab_id = probe_browser
    worker.evaluate("tabId => chrome.scripting.executeScript({target:{tabId}, "
                    "func:() => window.__tcAudioProbe.stop()})", tab_id)
    banner = page.locator("#tuneconsole-audio-probe")
    expect(banner).to_have_count(0)
    page.locator("video").evaluate("media => media.play()")
    worker.evaluate("tuneConsole.setup.install()")
    expect(banner).to_have_count(0)
    page.wait_for_function("() => !document.querySelector('video').paused && document.querySelector('video').currentTime > 0")
    assert _resources(worker, tab_id)["streams"] == 0 and _resources(worker, tab_id)["contexts"] == 0
    _released(worker, tab_id)


def test_stop_during_offline_decode_discards_late_pcm_and_result(probe_browser):
    context, worker, page, tab_id = probe_browser
    worker.evaluate("""tabId => chrome.scripting.executeScript({target:{tabId}, func:() => {
      const decode = OfflineAudioContext.prototype.decodeAudioData;
      OfflineAudioContext.prototype.decodeAudioData = async function(...args) {
        const pcm = await decode.apply(this, args);
        document.getElementById('tuneconsole-audio-probe').dataset.decodePending = 'true';
        await new Promise(resolve => window.releaseProbeDecode = resolve);
        return pcm;
      };
    }})""", tab_id)
    page.locator("video").evaluate("media => media.play()")
    banner = page.locator("#tuneconsole-audio-probe")
    expect(banner).to_have_attribute("data-decode-pending", "true", timeout=10000)
    banner.get_by_role("button", name="Stop check", exact=True).click()
    expect(banner.get_by_role("status")).to_contain_text("Audio check stopped")
    worker.evaluate("""tabId => chrome.scripting.executeScript({target:{tabId}, func:async () => {
      window.releaseProbeDecode(); await new Promise(resolve => setTimeout(resolve, 0));
    }})""", tab_id)
    assert _report(page)["outcome"] == "stopped"
    _released(worker, tab_id)
    assert page.locator("video").evaluate("media => !media.paused")


@pytest.mark.parametrize("entry", ["playing_event", "already_playing"])
@pytest.mark.parametrize("probe_browser", ["no_activation"], indirect=True)
def test_capture_and_pcm_decode_need_no_page_activation_or_audio_unlock(probe_browser, entry):
    context, worker, page, tab_id = probe_browser
    # Playwright evaluate normally supplies a debugger user gesture. Explicitly
    # turn it off. Muted media may play under Chrome's default autoplay rules;
    # element capture still exposes its pre-volume PCM. This profile has never
    # received any click, key, media permission or autoplay-policy override.
    session = context.new_cdp_session(page)
    worlds = []
    session.on("Runtime.executionContextCreated", lambda event: worlds.append(event["context"]["id"]))
    try:
        session.send("Runtime.enable")
        ready = session.send("Runtime.evaluate", {
            "expression": "new Promise(resolve => {const timer=setInterval(() => {"
                          "if(document.getElementById('tuneconsole-audio-probe')) {clearInterval(timer);resolve(true);}"
                          "},20);setTimeout(() => {clearInterval(timer);resolve(false);},5000);})",
            "userGesture": False, "awaitPromise": True, "returnByValue": True,
        })
        assert ready["result"]["value"] is True
        probe_world = None
        for world in worlds:
            found = session.send("Runtime.evaluate", {
                "expression": "!!window.__tcAudioProbe", "contextId": world,
                "userGesture": False, "returnByValue": True,
            })
            if found["result"].get("value") is True:
                probe_world = world
                break
        assert probe_world is not None
        session.send("Runtime.evaluate", {
            "expression": "window.AudioContext=class {constructor(){throw Error('Live AudioContext forbidden');}};"
                          "navigator.mediaDevices.getDisplayMedia=() => {throw Error('Sharing forbidden');};"
                          "navigator.mediaDevices.getUserMedia=() => {throw Error('Microphone forbidden');};",
            "contextId": probe_world, "userGesture": False,
        })
        if entry == "already_playing":
            session.send("Runtime.evaluate", {"expression": "__tcAudioProbe.stop()",
                "contextId": probe_world, "userGesture": False})
        playback = session.send("Runtime.evaluate", {
            "expression": "(async () => {const media=document.querySelector('video'); "
                          "media.muted=true; await media.play(); return {playing:!media.paused, "
                          "active:navigator.userActivation.isActive, ever:navigator.userActivation.hasBeenActive};})()",
            "userGesture": False, "awaitPromise": True, "returnByValue": True,
        })
        assert playback["result"]["value"] == {"playing": True, "active": False, "ever": False}
        if entry == "already_playing":
            session.send("Runtime.evaluate", {"expression": (EXT / "audio_probe.js").read_text(),
                "contextId": probe_world, "userGesture": False})
        completed = session.send("Runtime.evaluate", {
            "expression": "new Promise(resolve => {const timer=setInterval(() => {"
                          "const text=document.getElementById('tuneconsole-audio-probe')?.shadowRoot"
                          "?.querySelector('pre')?.textContent;"
                          "if(text) {clearInterval(timer);resolve(JSON.parse(text));}"
                          "},20);setTimeout(() => {clearInterval(timer);resolve(null);},12000);})",
            "userGesture": False, "awaitPromise": True, "returnByValue": True,
        })
        report = completed["result"]["value"]
    finally:
        session.detach()
    assert report is not None
    assert report["trigger"] == "automatic"
    assert report["user_activation_at_start"] == {"is_active": False, "has_been_active": False}
    assert report["realtime_audio_context_used"] is False and report["outcome"] == "available"
    assert report["rms"] == pytest.approx(.1 / 2**.5, abs=.015)
    assert report["samples_inspected"] > 100000
