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
        <ytmusic-player-bar>
          <span class="title">Previous song</span>
          <a href="/watch?v=previous&amp;list=OLD">Previous song</a>
          <button id="play-pause-button">Play</button>
        </ytmusic-player-bar>
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
      window.ensurePlayback = (target = {}) => new Promise(resolve => {
        const asyncResponse = bridgeListener({type: 'ensure-playing', ...target}, {}, resolve);
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
        assert result["playing"] is False, "delivery without a player is not success"
        page.wait_for_selector("video", state="attached")
        result = page.evaluate("ensurePlayback()")
    assert result["playing"] is True
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
    assert page.evaluate("ensurePlayback()")["playing"] is False
    assert page.evaluate("barClicks") == 0


def test_buffered_but_stalled_media_does_not_confirm_playback(paused_player):
    page = paused_player
    page.evaluate("""() => {
      const v = document.querySelector('video');
      Object.defineProperty(v, 'paused', {value: false});
      Object.defineProperty(v, 'readyState', {value: 2});
      Object.defineProperty(v, 'currentTime', {value: 0});
    }""")
    assert page.evaluate("ensurePlayback()")["playing"] is False
    assert page.evaluate("barClicks") == 0


@pytest.mark.parametrize("old_link", ["/watch?v=previous&list=PLREQUESTED", "/watch?v=requested&list=OLD"])
def test_watch_navigation_waits_for_the_requested_song_and_queue(paused_player, old_link):
    page = paused_player
    page.eval_on_selector("ytmusic-player-bar a", "(link, href) => link.href = href", old_link)
    target = {"videoId": "requested", "playlist": "PLREQUESTED"}
    result = page.evaluate("target => ensurePlayback(target)", target)
    assert result["playing"] is False
    assert page.evaluate("document.querySelector('video').paused") is True
    assert page.evaluate("barClicks + shelfClicks") == 0

    page.eval_on_selector("ytmusic-player-bar a", "link => link.href = '/watch?v=requested&list=RDAMPLPLREQUESTED'")
    result = page.evaluate("target => ensurePlayback(target)", target)
    assert result == {"playing": True, "videoId": "requested", "playlist": "RDAMPLPLREQUESTED"}
    assert page.evaluate("document.querySelector('video').paused") is False


@pytest.mark.parametrize("path", ["/browse/album", "/playlist?list=ALBUM"])
@pytest.mark.parametrize("previous_playing", [False, True])
def test_browse_page_starts_its_header_queue_instead_of_resuming_the_previous_song(paused_player, path, previous_playing):
    page = paused_player
    page.evaluate("""async ({path, previousPlaying}) => {
      history.replaceState({}, '', path);
      if (previousPlaying) await nativePlay();
      const header = document.createElement('ytmusic-responsive-header-renderer');
      header.innerHTML = '<ytmusic-play-button-renderer><button>Play album</button></ytmusic-play-button-renderer>';
      document.body.append(header);
      window.headerClicks = 0;
      header.querySelector('button').onclick = () => {
        headerClicks++;
        document.querySelector('ytmusic-player-bar a').href = '/watch?v=album-first&list=ALBUM';
        nativePlay();
      };
      document.querySelector('video').play = () => { throw Error('Must start the requested album'); };
    }""", {"path": path, "previousPlaying": previous_playing})
    target = {"url": "https://music.youtube.com" + path}
    if path.startswith("/playlist"):
        target["playlist"] = "ALBUM"
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is True
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is True
    assert page.evaluate("headerClicks") == 1
    assert page.evaluate("barClicks + shelfClicks") == 0


def test_album_launch_waits_for_its_header_and_then_for_its_player(paused_player):
    page = paused_player
    page.evaluate("history.replaceState({}, '', '/playlist?list=ALBUM')")
    target = {"url": "https://music.youtube.com/playlist?list=ALBUM", "playlist": "ALBUM"}
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is False
    assert page.evaluate("barClicks + shelfClicks") == 0
    page.evaluate("""() => {
      const header = document.createElement('ytmusic-responsive-header-renderer');
      header.innerHTML = '<ytmusic-play-button-renderer><button>Play album</button></ytmusic-play-button-renderer>';
      document.body.append(header);
      window.headerClicks = 0;
      header.querySelector('button').onclick = () => headerClicks++;
    }""")
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is False
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is False
    assert page.evaluate("headerClicks") == 1
    assert page.evaluate("barClicks + shelfClicks") == 0

    page.eval_on_selector("ytmusic-player-bar a", "link => link.href = '/watch?v=album-first&list=ALBUM'")
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is True
    assert page.evaluate("headerClicks") == 1


def test_album_launch_does_not_click_a_different_browse_page(paused_player):
    page = paused_player
    page.evaluate("history.replaceState({}, '', '/playlist?list=OTHER')")
    target = {"url": "https://music.youtube.com/playlist?list=ALBUM", "playlist": "ALBUM"}
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is False
    assert page.evaluate("barClicks + shelfClicks") == 0


def test_album_launch_retries_a_disabled_header_then_recovers_blocked_playback(paused_player):
    page = paused_player
    page.evaluate("""() => {
      history.replaceState({}, '', '/playlist?list=ALBUM');
      const header = document.createElement('ytmusic-responsive-header-renderer');
      header.innerHTML = '<ytmusic-play-button-renderer><button disabled>Play album</button></ytmusic-play-button-renderer>';
      document.body.append(header);
      window.headerClicks = 0;
      header.querySelector('button').onclick = () => {
        headerClicks++;
        document.querySelector('ytmusic-player-bar a').href = '/watch?v=album-first&list=ALBUM';
      };
      document.querySelector('video').play = () => Promise.reject(new DOMException('blocked', 'NotAllowedError'));
    }""")
    target = {"url": "https://music.youtube.com/playlist?list=ALBUM", "playlist": "ALBUM"}
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is False
    assert page.evaluate("headerClicks + barClicks + shelfClicks") == 0
    page.eval_on_selector("ytmusic-responsive-header-renderer button", "button => button.disabled = false")
    assert page.evaluate("target => ensurePlayback(target)", target)["playing"] is True
    assert page.evaluate("headerClicks") == 1
    assert page.evaluate("barClicks") == 1
    assert page.evaluate("shelfClicks") == 0
