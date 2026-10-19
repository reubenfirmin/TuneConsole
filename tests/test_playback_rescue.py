"""#101 Playback rescue: show the YouTube Music tab, briefly, when a background swap did not start
playing, then put the owner back.

navigateYtmTab deliberately swaps the tab in the background so you stay on TuneConsole, but Chrome
gates unmuted playback on a gesture or a high media-engagement score, and a tab that was never
foregrounded often loses that bet: the swap lands silently and nothing plays. Exercise focus changes
with the actual service worker in Node, using fake Chrome tabs and controllable timers.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "extension"


def _bg():
    return (EXT / "background.js").read_text()


def test_navigate_arms_the_rescue_on_both_paths():
    src = _bg()
    assert "armPlaybackRescue" in src
    # Both branches of navigateYtmTab need it: reusing an existing background tab AND opening a new
    # background one (a brand-new tab is even less likely to autoplay).
    nav = src[src.index("async function navigateYtmTab"):src.index("async function suppressBeforeUnload")]
    assert nav.count("armPlaybackRescue") == 2, "both the reuse and create branches must arm it"


def test_rescue_waits_before_showing_the_tab():
    """The common case must never flicker: a normal navigate + autoplay reports in well under this."""
    src = _bg()
    assert "RESCUE_WAIT_MS = 2500" in src
    assert "RESCUE_DWELL_MS = 600" in src
    assert "RESCUE_TIMEOUT_MS = 6000" in src


def test_a_playing_frame_cancels_the_rescue():
    """The whole point of "if needed": a real playing frame means the swap worked, so never switch."""
    src = _bg()
    assert "cancelPlaybackRescue" in src
    # Cancellation keys off a play frame that is actually PLAYING. content.js reports paused off the
    # real <video>, and a paused play frame must not count as success.
    assert "!msg.paused" in src


@pytest.mark.parametrize("scenario", [
    "app_silent", "app_playing", "app_other_window", "manual_switch", "already_youtube",
    "other_tab_silent", "other_tab_playing", "playing_before_wait", "app_before_nudge",
    "closed_return_tab", "superseded", "late_player", "missing_receiver", "pending_reply",
    "manual_switch_during_retry", "closed_youtube",
])
def test_rescue_restores_tuneconsole_without_overriding_user_navigation(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const scenario = process.argv[1];
const tabs = new Map([
  [1, { id: 1, windowId: 10, url: "http://127.0.0.1:8765/clusters", active: true }],
  [2, { id: 2, windowId: scenario === "app_other_window" ? 20 : 10,
        url: "https://music.youtube.com/watch?v=song", active: false }],
  [3, { id: 3, windowId: 10, url: "https://example.org/", active: false }],
]);
let focusedWindow = 10;
const activations = [], messages = [], timers = new Map(), injections = [];
let timerId = 0, onMessage, onRemoved, now = 0, attempts = 0;
const event = () => ({ addListener() {} });
const activate = id => {
  const tab = tabs.get(id);
  assert(tab, "tab was closed");
  for (const other of tabs.values()) if (other.windowId === tab.windowId) other.active = other.id === id;
};
const activeTab = () => [...tabs.values()].find(t => t.active && t.windowId === focusedWindow)?.id;
const play = () => onMessage({ type: "play", paused: false }, { tab: tabs.get(2) });
const context = vm.createContext({
  URL, console: { log() {}, warn() {} },
  Date: class extends Date { static now() { return now; } },
  setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, at: now + delay }); return id; },
  clearTimeout: id => timers.delete(id),
  WebSocket: class { static CLOSED = 3; static OPEN = 1; readyState = 0; },
  chrome: {
    tabs: {
      query: async () => { const tab = tabs.get(activeTab()); return tab ? [{ ...tab }] : []; },
      update: async (id, changes) => {
        if (!tabs.has(id)) throw new Error("tab closed");
        if (changes.active) { activate(id); activations.push(id); }
        return { ...tabs.get(id) };
      },
      sendMessage: async (id, msg) => {
        messages.push({ id, type: msg.type });
        attempts++;
        if (!tabs.has(id)) throw new Error("tab closed");
        if (scenario === "pending_reply") return new Promise(() => {});
        if (scenario === "missing_receiver" && !injections.length) throw new Error("no receiver");
        const playing = ["app_playing", "other_tab_playing"].includes(scenario) ||
          (["late_player", "manual_switch_during_retry"].includes(scenario) && attempts >= 3) ||
          (scenario === "missing_receiver" && injections.length > 0);
        if (playing) play();
        return { playing };
      },
      onUpdated: event(), onRemoved: { addListener: fn => { onRemoved = fn; } },
    },
    windows: {
      update: async id => { focusedWindow = id; },
      onRemoved: event(),
    },
    scripting: { executeScript: async details => { injections.push(details); } },
    storage: { session: { get: async () => ({}) } },
    runtime: {
      onMessage: { addListener: fn => { onMessage = fn; } },
      onStartup: event(), onInstalled: event(),
    },
    alarms: { create() {}, onAlarm: event() },
  },
});
vm.runInContext(fs.readFileSync("extension/background.js", "utf8"), context);
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const advance = async elapsed => {
  const until = now + elapsed;
  for (;;) {
    const entry = [...timers].filter(([, timer]) => timer.at <= until)
      .sort((a, b) => a[1].at - b[1].at)[0];
    if (!entry) break;
    timers.delete(entry[0]);
    now = entry[1].at;
    await entry[1].fn();
    await flush();
  }
  now = until;
  await flush();
};
(async () => {
  if (scenario.startsWith("other_tab") || scenario === "app_before_nudge") activate(3);
  if (scenario === "already_youtube") activate(2);
  context.armPlaybackRescue(2);
  if (scenario === "app_before_nudge") activate(1);
  if (scenario === "playing_before_wait") play();
  await advance(2500);
  if (scenario === "playing_before_wait") {
    assert.deepEqual(activations, []);
    assert.deepEqual(messages, []);
    assert.equal(activeTab(), 1);
    return;
  }
  assert.equal(activeTab(), 2);
  assert.deepEqual(messages, [{ id: 2, type: "ensure-playing" }]);
  if (scenario === "manual_switch") activate(3);
  if (scenario === "closed_return_tab") tabs.delete(1);
  if (scenario === "closed_youtube") { tabs.delete(2); activate(1); onRemoved(2); }
  if (scenario === "superseded") context.armPlaybackRescue(2);
  await advance(600);
  if (["app_silent", "app_other_window", "app_before_nudge", "late_player", "pending_reply",
       "manual_switch_during_retry"].includes(scenario)) {
    assert.equal(activeTab(), 2, "do not leave after 600 ms without confirmed playback");
  }
  if (scenario === "manual_switch_during_retry") activate(3);
  if (scenario === "superseded") {
    assert.deepEqual(activations, [2]);
    return; // the cancelled episode must not restore its old tab
  }
  await advance(6000);
  if (["app_silent", "app_playing", "app_other_window", "app_before_nudge", "late_player",
       "missing_receiver", "pending_reply"].includes(scenario)) {
    assert.equal(activeTab(), 1);
    assert.equal(focusedWindow, 10);
    assert.deepEqual(activations, [2, 1]);
  } else if (scenario === "other_tab_playing") {
    assert.equal(activeTab(), 3);
    assert.deepEqual(activations, [2, 3]);
  } else if (scenario === "closed_youtube") {
    assert.equal(activeTab(), 1);
    assert.deepEqual(activations, [2]);
  } else {
    assert.equal(activeTab(), ["manual_switch", "manual_switch_during_retry"].includes(scenario) ? 3 : 2);
    assert.deepEqual(activations, [2]);
  }
  if (scenario === "late_player") assert.equal(attempts, 3);
  if (scenario === "missing_receiver") {
    assert.equal(injections.length, 3, "repair all guarded content/sensor scripts");
    assert.equal(attempts, 2);
  }
  if (scenario === "pending_reply") assert.equal(attempts, 1, "do not stack pending requests");
  const previous = attempts;
  await advance(10000);
  assert.equal(attempts, previous, "stop retrying after success or deadline");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=EXT.parent,
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


def test_closing_a_tab_clears_its_rescue_state():
    """playingTabs would otherwise grow for the life of the service worker: the deck branch of
    onRemoved returns early for non-deck tabs, which is most of them."""
    src = _bg()
    removed = src[src.index("chrome.tabs.onRemoved.addListener"):]
    removed = removed[:removed.index("\n});")]
    assert "playingTabs.delete(tabId)" in removed
    assert "cancelPlaybackRescue" in removed


def test_rescue_targets_the_window_the_owner_was_last_in():
    """A service worker has no window of its own, so currentWindow has nothing to resolve against
    and the return hop would silently never happen."""
    src = _bg()
    assert "lastFocusedWindow: true" in src
    assert "currentWindow: true" not in src


def test_rescue_asks_the_tab_to_start_playing_after_foregrounding():
    """Foregrounding often is not enough on its own: once the tab is visible we also nudge the page to
    actually play, rather than leave the owner staring at a paused player."""
    src = _bg()
    rescue = src[src.index("async function runPlaybackRescue"):]
    rescue = rescue[:rescue.index("\n}")]
    assert "attemptPlaybackRescue(episode)" in rescue
    # order matters: activate the tab (visibility helps autoplay), THEN ask it to play.
    assert rescue.index("active: true") < rescue.index("attemptPlaybackRescue(episode)")


def _content():
    return (EXT / "content.js").read_text()


def test_content_handles_ensure_playing():
    src = _content()
    assert 'msg.type === "ensure-playing"' in src


def test_ensure_playing_clicks_the_page_play_button_as_a_fallback():
    """The button the owner pointed at: the page's main play control, used when a direct play() is
    rejected or there is no <video> yet."""
    src = _content()
    assert "ytmusic-play-button-renderer" in src
    """The radio decks have their own waiting-state net (deckWaitingFocused + deck-waiting pevents).
    The rescue is for the plain play path only, and must not double up on deck tabs."""
    src = _bg()
    rescue = src[src.index("async function runPlaybackRescue"):]
    rescue = rescue[:rescue.index("\n}")]
    assert "deckWindowId" not in rescue and "toggleDecks" not in rescue
