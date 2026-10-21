"""Exercise playback ownership and playlist replacement with the real service worker."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["audible", "heartbeat", "pending", "concurrent", "no_music", "leaving"])
def test_playlist_launch_reuses_and_controls_the_playing_tab(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); }});
const app = {id: 1, url: 'http://127.0.0.1:8765/', active: true};
const old = {id: 2, url: 'https://music.youtube.com/watch?v=autoplay&list=RDAMVMautoplay',
             active: false, audible: scenario === 'audible', lastAccessed: 1};
const browsing = {id: 3, url: 'https://music.youtube.com/', active: true, lastAccessed: 100};
let tabs = [app, old, browsing];
if (scenario === 'pending') {
  tabs = [app, {id: 2, url: 'about:blank', pendingUrl: old.url}];
}
if (['concurrent', 'no_music'].includes(scenario)) {
  tabs = [app, {id: 8, url: 'https://music.youtube.com.evil.example/'}];
}
if (scenario === 'leaving') tabs = [app, {...old, pendingUrl: 'https://example.org/'}];
const messages = [], frames = [], created = [], updated = [];
const runtime = {onMessage: event(), onStartup: event(), onInstalled: event()};
const sockets = [];
class Socket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor() { this.readyState = 0; sockets.push(this); }
  send(frame) { frames.push(JSON.parse(frame)); }
}
const chrome = {
  runtime,
  tabs: {
    query: async ({url} = {}) => tabs.filter(t => !url || t.url.startsWith('https://music.youtube.com/')),
    update: async (id, changes) => {
      const tab = tabs.find(t => t.id === id);
      assert(tab);
      updated.push({id, ...changes});
      Object.assign(tab, changes);
      if (changes.url) delete tab.pendingUrl;
      return tab;
    },
    create: async properties => {
      created.push(properties);
      // Chrome has not committed the first document yet when another launch arrives.
      const tab = {id: 10, url: 'about:blank', pendingUrl: properties.url};
      tabs.push(tab);
      return tab;
    },
    sendMessage: async (id, msg) => {
      messages.push({id, ...msg});
      return {ok: true};
    },
    onUpdated: event(), onRemoved: event(),
  },
  scripting: {executeScript: async () => {}},
  storage: {session: {get: async () => ({})}},
  windows: {onRemoved: event()},
  alarms: {create() {}, onAlarm: event()},
};
const context = vm.createContext({chrome, WebSocket: Socket, URL,
  console: {log() {}, warn() {}}, setTimeout() {}, clearTimeout() {},
});
vm.runInContext(fs.readFileSync('extension/background.js', 'utf8'), context);
sockets[0].readyState = Socket.OPEN;
const report = (id, msg) => {
  for (const listener of runtime.onMessage.listeners) listener(msg, {tab: tabs.find(t => t.id === id)});
};
const next = 'https://music.youtube.com/watch?list=PL_new';
(async () => {
  report(2, {type: 'play', title: 'Song', paused: false, timeZone: 'Pacific/Kiritimati'});
  assert.equal(frames.at(-1).timeZone, 'Pacific/Kiritimati', 'preserve the content sensor timezone');
  if (scenario === 'heartbeat') {
    report(2, {type: 'now-heartbeat', title: 'Autoplay', paused: false});
    const count = frames.length;
    report(3, {type: 'now-heartbeat', title: 'Old playlist', paused: true});
    report(3, {type: 'pevent', kind: 'bye'});
    assert.equal(frames.length, count, 'a paused/browsed tab cannot replace or clear Now Playing');
    report(2, {type: 'now-heartbeat', title: 'Autoplay', paused: true});
    vm.runInContext('liveTabId = 2; standbyTabId = 3;', context);
    report(3, {type: 'now-heartbeat', title: 'Standby', paused: false});
    assert.equal((await context.pickYtmTab()).id, 2, 'standby cannot take playback ownership');
  }
  if (['audible', 'heartbeat'].includes(scenario)) {
    await context.playPauseInYtmTab();
    await context.rateInYtmTab('like');
    assert.deepEqual(messages.map(m => [m.id, m.type]), [[2, 'playpause'], [2, 'rate']]);
  }
  if (scenario === 'pending') assert.equal((await context.ensureYtmTab()).id, 2);
  if (scenario === 'concurrent') {
    await Promise.all([context.navigateYtmTab(next), context.navigateYtmTab(next + '_second')]);
    assert.equal(created.length, 1, 'overlapping launches create a single music tab');
    assert.equal(tabs.find(t => t.id === 10).url, next + '_second');
    return;
  }
  await context.navigateYtmTab(next);
  if (['no_music', 'leaving'].includes(scenario)) {
    assert.equal(created.length, 1);
    assert.equal(created[0].url, next);
    return;
  }
  assert.equal(created.length, 0, 'replace autoplay in its existing tab');
  assert.equal(tabs.find(t => t.id === 2).url, next);
  assert(updated.every(change => !change.active), 'keep the owner on TuneConsole');
  if (scenario !== 'pending') {
    assert(messages.some(m => m.id === 3 && m.type === 'deck-pause'), 'silence the other music tab');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
