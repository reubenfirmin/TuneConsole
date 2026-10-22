"""#101 Playback rescue: show the YouTube Music tab, briefly, when a background swap did not start
playing, then put the owner back.

Playback deliberately swaps the tab in the background so you stay on TuneConsole, but Chrome
gates unmuted playback on a gesture or a high media-engagement score, and a tab that was never
foregrounded often loses that bet: the swap lands silently and nothing plays. Exercise focus changes
with the actual service worker in Node, using fake Chrome tabs and controllable timers.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "extension"


@pytest.mark.parametrize("scenario", [
    "app_silent", "app_playing", "app_other_window", "manual_switch", "already_youtube",
    "other_tab_silent", "other_tab_playing", "playing_before_wait", "app_before_nudge",
    "closed_return_tab", "superseded", "late_player", "missing_receiver", "pending_reply",
    "manual_switch_during_retry", "closed_youtube", "new_tab", "paused_before_wait",
    "old_play_before_wait", "stalled_before_wait", "unverified_before_wait", "wrong_queue_before_wait",
])
def test_rescue_restores_tuneconsole_without_overriding_user_navigation(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require("node:assert/strict");
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
const play = () => {
  tabs.get(2).audible = true;
  onMessage({type: 'play', paused: false, videoId: 'requested', playlist: 'PLREQUESTED',
    metadataVerified: true}, {tab: tabs.get(2)});
};
const context = vm.createContext({
  URL, console: { log() {}, warn() {} },
  Date: class extends Date { static now() { return now; } },
  setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, at: now + delay }); return id; },
  clearTimeout: id => timers.delete(id),
  WebSocket: class { static CLOSED = 3; static OPEN = 1; readyState = 0; },
  chrome: {
    tabs: {
      query: async (query = {}) => {
        if (query.active) {
          assert.equal(query.lastFocusedWindow, true, 'a worker has no current window');
          assert(!('currentWindow' in query));
          return [...tabs.values()].filter(t => t.active && t.windowId === focusedWindow).map(t => ({...t}));
        }
        return [...tabs.values()].map(t => ({...t}));
      },
      update: async (id, changes) => {
        if (!tabs.has(id)) throw new Error("tab closed");
        if (changes.active) { activate(id); activations.push(id); }
        if (changes.url) tabs.get(id).url = changes.url;
        return { ...tabs.get(id) };
      },
      create: async changes => {
        assert.equal(changes.active, false, 'launch a new player in the background');
        const tab = {id: 2, windowId: 10, ...changes};
        tabs.set(tab.id, tab);
        return {...tab};
      },
      sendMessage: async (id, msg) => {
        if (attempts === 0) assert.equal(activeTab(), id, 'foreground the player before asking it to play');
        messages.push({ id, type: msg.type });
        attempts++;
        if (!tabs.has(id)) throw new Error("tab closed");
        if (scenario === "pending_reply") return new Promise(() => {});
        if (scenario === "missing_receiver" && !injections.length) throw new Error("no receiver");
        const playing = ["app_playing", "other_tab_playing"].includes(scenario) ||
          (["late_player", "manual_switch_during_retry"].includes(scenario) && attempts >= 3) ||
          (scenario === "missing_receiver" && injections.length > 0);
        if (playing) play();
        assert.equal(msg.videoId, 'requested');
        assert.equal(msg.playlist, 'PLREQUESTED');
        return {playing, videoId: 'requested', playlist: 'PLREQUESTED'};
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
const {playback} = require('./tests/extension_worker.cjs').loadWorker(context);
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
  if (scenario === "new_tab") tabs.delete(2);
  const target = 'https://music.youtube.com/watch?v=requested&list=PLREQUESTED';
  await playback.navigate(target);
  assert.equal(tabs.get(2).url, target);
  injections.length = 0; // The pre-navigation beforeunload guard is separate from sensor repair.
  if (scenario === "app_before_nudge") activate(1);
  if (scenario === "playing_before_wait") { play(); await flush(); }
  if (scenario === "paused_before_wait") {
    onMessage({type: 'play', paused: true}, {tab: tabs.get(2)});
    await flush();
  }
  if (['old_play_before_wait', 'stalled_before_wait', 'unverified_before_wait', 'wrong_queue_before_wait'].includes(scenario)) {
    tabs.get(2).audible = scenario !== 'stalled_before_wait';
    onMessage({type: 'now-heartbeat', paused: false,
      videoId: scenario === 'old_play_before_wait' ? 'previous' : 'requested',
      playlist: scenario === 'wrong_queue_before_wait' ? 'OLDQUEUE' : 'PLREQUESTED',
      metadataVerified: scenario !== 'unverified_before_wait'}, {tab: tabs.get(2)});
    await flush();
  }
  await advance(2499);
  assert.deepEqual(activations, [], 'normal autoplay gets 2500 ms without a focus change');
  assert.deepEqual(messages, [], 'activate the tab before asking it to play');
  await advance(1);
  if (scenario === "playing_before_wait") {
    assert.deepEqual(activations, []);
    assert.deepEqual(messages, []);
    assert.equal(activeTab(), 1);
    assert.equal(timers.size, 0, 'audible playback of the requested song cancels the rescue timer');
    return;
  }
  assert.equal(activeTab(), 2);
  assert.deepEqual(messages, [{ id: 2, type: "ensure-playing" }]);
  if (scenario === "manual_switch") activate(3);
  if (scenario === "closed_return_tab") tabs.delete(1);
  if (scenario === "closed_youtube") { tabs.delete(2); activate(1); onRemoved(2); }
  if (scenario === "superseded") await playback.navigate(target);
  await advance(599);
  if (['app_playing', 'other_tab_playing', 'missing_receiver'].includes(scenario)) {
    assert.equal(activeTab(), 2, 'confirmed playback still gets at least 600 ms in front');
  }
  await advance(1);
  if (["app_silent", "app_other_window", "app_before_nudge", "late_player", "pending_reply",
       "manual_switch_during_retry", "new_tab", "paused_before_wait", "old_play_before_wait",
       "stalled_before_wait", "unverified_before_wait", "wrong_queue_before_wait"].includes(scenario)) {
    assert.equal(activeTab(), 2, "do not leave after 600 ms without confirmed playback");
  }
  if (scenario === "manual_switch_during_retry") activate(3);
  if (scenario === "superseded") {
    assert.deepEqual(activations, [2]);
    return; // the cancelled episode must not restore its old tab
  }
  await advance(5399);
  if (['app_silent', 'pending_reply', 'new_tab', 'paused_before_wait', 'old_play_before_wait',
       'stalled_before_wait', 'unverified_before_wait', 'wrong_queue_before_wait'].includes(scenario)) {
    assert.equal(activeTab(), 2, 'allow six seconds for a foregrounded player to respond');
  }
  await advance(1);
  if (["app_silent", "app_playing", "app_other_window", "app_before_nudge", "late_player",
       "missing_receiver", "pending_reply", "new_tab", "paused_before_wait", "old_play_before_wait",
       "stalled_before_wait", "unverified_before_wait", "wrong_queue_before_wait"].includes(scenario)) {
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
  assert.equal(timers.size, 0, 'completion or removal clears every rescue timer');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=EXT.parent,
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


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
