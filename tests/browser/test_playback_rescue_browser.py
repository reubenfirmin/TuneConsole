"""Run the real content script against paused media and representative YTM controls."""
import base64
import io
import wave
from pathlib import Path

import pytest


CONTENT = Path(__file__).resolve().parents[2] / "extension" / "content.js"


@pytest.fixture
def paused_player(page):
    # Local playable media keeps this deterministic without a YouTube login or external requests.
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000 * 10)
    src = "data:audio/wav;base64," + base64.b64encode(audio.getvalue()).decode()
    page.route("https://music.youtube.com/**", lambda route: route.fulfill(
        content_type="text/html", body="""
        <ytmusic-play-button-renderer><button id="unrelated">Unrelated song</button></ytmusic-play-button-renderer>
        <video preload="auto"></video>
        <ytmusic-player-bar><button id="play-pause-button">Play</button></ytmusic-player-bar>
        <button id="prime">Prepare paused player</button>
        """))
    page.goto("https://music.youtube.com/watch?v=requested")
    page.evaluate("""src => {
      const v = document.querySelector('video');
      v.src = src;
      window.nativePlay = v.play.bind(v);
      window.barClicks = 0;
      window.shelfClicks = 0;
      document.querySelector('#unrelated').onclick = () => shelfClicks++;
      document.querySelector('#play-pause-button').onclick = () => {
        barClicks++;
        if (v.paused) nativePlay(); else v.pause();
      };
      document.querySelector('#prime').onclick = async () => {
        await nativePlay();
        v.pause();
        window.primed = true;
      };
      window.chrome = {runtime: {
        onMessage: {addListener: fn => window.bridgeListener = fn},
        sendMessage: () => Promise.resolve(),
      }};
      window.ensurePlayback = () => new Promise(resolve => {
        const asyncResponse = bridgeListener({type: 'ensure-playing'}, {}, resolve);
        if (asyncResponse !== true) throw Error('Playback confirmation channel was closed');
      });
    }""", src)
    page.add_script_tag(path=str(CONTENT))
    page.get_by_role("button", name="Prepare paused player").click()
    page.wait_for_function("window.primed && document.querySelector('video').paused")
    return page


@pytest.mark.parametrize("mode", ["direct", "rejected", "pending", "already_playing", "late_controls"])
def test_paused_media_starts_and_confirms_without_clicking_an_unrelated_song(paused_player, mode):
    page = paused_player
    page.evaluate("""mode => {
      const v = document.querySelector('video');
      if (mode === 'rejected') v.play = () => Promise.reject(new DOMException('blocked', 'NotAllowedError'));
      if (mode === 'pending') v.play = () => new Promise(() => {});
      if (mode === 'already_playing') return nativePlay();
      if (mode === 'late_controls') {
        const bar = document.querySelector('ytmusic-player-bar');
        bar.remove();
        v.remove();
        setTimeout(() => document.body.append(v, bar), 700);
      }
    }""", mode)
    result = page.evaluate("ensurePlayback()")
    if mode == "late_controls":
        assert result == {"playing": False}, "delivery without a player is not success"
        page.wait_for_selector("video", state="attached")
        result = page.evaluate("ensurePlayback()")
    assert result == {"playing": True}
    assert page.evaluate("document.querySelector('video').paused") is False
    assert page.evaluate("shelfClicks") == 0
    assert page.evaluate("barClicks") == (1 if mode in {"rejected", "pending"} else 0)
    before = page.evaluate("document.querySelector('video').currentTime")
    page.wait_for_function("before => document.querySelector('video').currentTime > before", arg=before)


def test_buffering_is_not_confirmed_and_is_never_toggled_back_to_pause(paused_player):
    page = paused_player
    page.evaluate("""() => {
      const v = document.querySelector('video');
      Object.defineProperty(v, 'paused', {value: false});
      Object.defineProperty(v, 'readyState', {value: 0});
      v.play = () => { throw Error('Already starting'); };
    }""")
    assert page.evaluate("ensurePlayback()") == {"playing": False}
    assert page.evaluate("barClicks") == 0


def test_browse_page_starts_its_header_queue_instead_of_resuming_the_previous_song(paused_player):
    page = paused_player
    page.evaluate("""() => {
      history.replaceState({}, '', '/browse/album');
      const header = document.createElement('ytmusic-responsive-header-renderer');
      header.innerHTML = '<ytmusic-play-button-renderer><button>Play album</button></ytmusic-play-button-renderer>';
      document.body.append(header);
      window.headerClicks = 0;
      header.querySelector('button').onclick = () => { headerClicks++; nativePlay(); };
      document.querySelector('video').play = () => { throw Error('Must start the requested album'); };
    }""")
    assert page.evaluate("ensurePlayback()") == {"playing": True}
    assert page.evaluate("headerClicks") == 1
    assert page.evaluate("barClicks + shelfClicks") == 0
