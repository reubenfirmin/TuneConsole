"""Exercise the document status owner with the real dock, Home, and row consumers."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("path", ["/playlist/7", "/album?browse=album", "/"])
def test_status_is_shared_across_mounts_refreshes_and_recovery(path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise playback status")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const root = 'src/yt_playlist/web/static/';
const playerSource = fs.readFileSync(root + 'player-card.js', 'utf8');
const tableSource = fs.readFileSync(root + 'track-tables.js', 'utf8');
const homeSource = fs.readFileSync(root + 'home.js', 'utf8');
const statusSource = fs.readFileSync(root + 'playback-status.js', 'utf8');
const window = new EventTarget(), document = new EventTarget();
const homePage = process.argv[1] === '/';
const location = new URL('http://localhost' + process.argv[1]);
let table = {isConnected: true};
function row(vid) {
  const classes = new Set();
  return {vid, classList: {add: c => classes.add(c), remove: c => classes.delete(c),
    contains: c => classes.has(c)}, getAttribute: () => vid, closest: () => table};
}
let rows = [row('one'), row('two')];
document.readyState = 'loading';
document.body = new EventTarget();
document.getElementById = id => id === 'home' && homePage ? {} : null;
document.querySelectorAll = selector => selector === 'tr[data-vid]' ? rows : [];
document.querySelector = selector => selector === 'tr[data-vid]' ? rows[0] :
  rows.find(r => selector === 'tr[data-vid="' + r.vid + '"]') || null;
let nextTimer = 0;
const timers = new Map(), calls = [];
const interval = (fn, ms) => { timers.set(++nextTimer, {fn, ms}); return nextTimer; };
const clear = id => timers.delete(id);
const tick = ms => [...timers.values()].filter(t => t.ms === ms).forEach(t => t.fn());
const count = url => calls.filter(c => c.url === url).length;
const snapshot = vid => ({connected: true, radio: true, radio_ready: true,
  sensor_health: {verified: true}, now_playing: {video_id: vid, title: vid, artist: 'Artist',
    paused: false, likeStatus: 'LIKE', playlist_ytm_id: 'PLRADIO', source: {kind: 'radio'}}});
let response = () => Promise.resolve({ok: true, json: async () => snapshot('one')});
function fetch(url, options) {
  calls.push({url, options});
  if (url === '/bridge/status') return response(options);
  if (url.endsWith('/cells')) return Promise.resolve({json: async () => ({cells: [], pending: 1})});
  if (url === '/sync/active' || url === '/sync/recent') return Promise.resolve({json: async () => ({})});
  throw Error('Unexpected request ' + url);
}
const context = vm.createContext({window, document, location, URL, CustomEvent, AbortController,
  fetch, console, setInterval: interval, clearInterval: clear, setTimeout: interval, clearTimeout: clear});
vm.runInContext(statusSource, context);
vm.runInContext(playerSource, context);
vm.runInContext(tableSource, context);
vm.runInContext(homeSource, context);
function mountDock() {
  const dock = vm.runInContext('playerStatus()', context);
  const listener = e => dock.apply(e.detail);
  window.addEventListener('bridge-status', listener);
  dock.init(); dock.init();
  return {dock, unmount: () => window.removeEventListener('bridge-status', listener)};
}
const first = mountDock();
let home;
if (homePage) {
  home = vm.runInContext('homeStatus()', context);
  window.addEventListener('bridge-status', e => home.applyBridgeStatus(e.detail));
  home.init(); home.init();
}
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  document.dispatchEvent(new Event('DOMContentLoaded'));
  await flush();
  assert.equal(count('/bridge/status'), 1);
  assert.equal(first.dock.nowPlaying.video_id, 'one');
  assert(rows[0].classList.contains('now-playing'));
  if (home) assert.equal(home.radioPlaybackState, 'playing');
  const period = homePage ? 3000 : 2500;
  response = () => Promise.resolve({ok: true, json: async () => snapshot('two')});
  tick(period); await flush();
  assert.equal(count('/bridge/status'), 2, 'one request per interval with every consumer mounted');
  assert.equal(first.dock.nowPlaying.video_id, 'two');
  assert(rows[1].classList.contains('now-playing'));
  if (home) {
    assert.equal(home.nowPlaying.video_id, 'two');
    home.nowPlaying.likeStatus = 'DISLIKE';
    assert.equal(window.playbackStatus.snapshot.now_playing.likeStatus, 'LIKE');
  }
  // A new table and dock receive the cached snapshot without creating another status loop.
  first.unmount();
  table.isConnected = false;
  table = {isConnected: true}; rows = [row('one'), row('two')];
  document.dispatchEvent(new Event('htmx:afterSwap'));
  const replacement = mountDock().dock;
  assert.equal(replacement.nowPlaying.video_id, 'two');
  assert(rows[1].classList.contains('now-playing'));
  const timerCount = timers.size;
  vm.runInContext(statusSource, context);
  vm.runInContext(tableSource, context);
  window.playbackStatus.start();
  assert.equal(timers.size, timerCount);
  assert.equal(count('/bridge/status'), 2);
  for (const failure of [
    () => Promise.reject(Error('offline')),
    () => Promise.resolve({ok: false}),
    () => Promise.resolve({ok: true, json: async () => { throw Error('invalid JSON'); }}),
    () => Promise.resolve({ok: true, json: async () => ({})}),
  ]) {
    response = failure;
    tick(period); await flush();
    assert.equal(replacement.connected, true, 'preserve known connection on a transient failure');
    assert.equal(replacement.nowPlaying, null);
    assert(rows.every(r => !r.classList.contains('now-playing')));
    if (home) {
      assert.equal(home.radioPlaybackState, '');
      assert.equal(home.sensorHealth, null);
    }
  }
  response = () => Promise.resolve({ok: true, json: async () => ({connected: false})});
  tick(period); await flush();
  assert.equal(replacement.connected, false);
  const cells = count('/playlist/7/cells');
  tick(2500); await flush();
  if (location.pathname.startsWith('/playlist/')) assert(count('/playlist/7/cells') > cells,
    'metadata continues while disconnected');
  response = () => Promise.resolve({ok: true, json: async () => snapshot('one')});
  tick(period); await flush();
  assert.equal(replacement.nowPlaying.video_id, 'one');
  assert(rows[0].classList.contains('now-playing'));
  if (home) assert.equal(home.radioPlaybackState, 'playing');
  // Slow requests cannot overlap. Suspending the page aborts them and ignores stale completion.
  let release, signal;
  response = options => { signal = options.signal; return new Promise(resolve => { release = resolve; }); };
  const before = count('/bridge/status');
  window.playbackStatus.refresh();
  tick(period); tick(period); window.playbackStatus.start();
  assert.equal(count('/bridge/status'), before + 1);
  window.dispatchEvent(new Event('pagehide'));
  assert.equal(signal.aborted, true);
  response = () => Promise.resolve({ok: true, json: async () => snapshot('two')});
  window.dispatchEvent(new Event('pageshow'));
  window.dispatchEvent(new Event('pageshow'));
  await flush();
  release({ok: true, json: async () => snapshot('one')}); await flush();
  assert.equal(count('/bridge/status'), before + 2);
  assert.equal(replacement.nowPlaying.video_id, 'two');
  assert(rows[1].classList.contains('now-playing'));
  window.playbackStatus.stop();
  home?.destroy();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, path], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
