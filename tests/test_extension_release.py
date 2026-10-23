"""Release regressions for uncertain delivery, extension updates, and multiple user tabs."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "missing_receiver", "lost_reply", "empty_reply", "failed_injection", "lost_retry_reply",
    "refresh_views", "install", "user_tabs", "audible", "playing", "paused", "unknown", "idle",
    "playpause_lost_reply", "rating_lost_reply", "next_lost_reply", "next_missing_receiver",
])
def test_release_tab_and_delivery_safety(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const scenario = process.argv[1];
const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); }});
const runtime = {onMessage: event(), onStartup: event(), onInstalled: event()};
const sends = [], injections = [], reloads = [], removed = [], activated = [];
let attempts = 0;
let tabs = [
  {id: 1, url: 'https://music.youtube.com/watch?v=playing', audible: scenario === 'audible'},
  {id: 2, url: 'https://music.youtube.com/browse/VLPL_edited'},
];
class Socket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor() { this.readyState = 0; }
}
const chrome = {
  runtime,
  cookies: {get: async () => ({value: 'test-cookie'})},
  tabs: {
    query: async ({url}) => Array.isArray(url) ? [] : tabs,
    get: async id => tabs.find(tab => tab.id === id),
    reload: async id => reloads.push(id),
    remove: async id => removed.push(id),
    update: async id => activated.push(id),
    onUpdated: event(), onRemoved: event(),
    sendMessage: async (id, msg) => {
      sends.push({id, type: msg.type, playlist: msg.playlist});
      if (msg.type === 'sensor-ping') return {ok: true, receiverRevision: 3, mainVersion: 4, mainRevision: 4, mainAgeMs: 0};
      if (['fetch', 'playpause', 'rate', 'next'].includes(msg.type)) {
        attempts += 1;
        if (['missing_receiver', 'failed_injection', 'lost_retry_reply', 'next_missing_receiver'].includes(scenario)
            && attempts === 1) {
          throw Error('Could not establish connection. Receiving end does not exist.');
        }
        if (['lost_reply', 'lost_retry_reply', 'playpause_lost_reply', 'rating_lost_reply', 'next_lost_reply'].includes(scenario)) {
          throw Error('The message port closed before a response was received.');
        }
        if (scenario === 'empty_reply') return undefined;
        return {ok: true, status: 200, body: '{}'};
      }
      return {ok: true};
    },
  },
  scripting: {executeScript: async call => {
    injections.push(call);
    if (scenario === 'failed_injection') throw Error('tab navigated away');
    if (call.func) {
      if (scenario === 'unknown') return [];
      const media = scenario === 'playing' ? {paused: false, ended: false, currentTime: 0}
        : scenario === 'paused' ? {paused: true, ended: false, currentTime: 42}
        : {paused: true, ended: false, currentTime: 0};
      return [{result: vm.runInNewContext('(' + call.func.toString() + ')()', {
        document: {querySelectorAll: () => [media]},
      })}];
    }
    return [];
  }},
  windows: {onRemoved: event()},
  alarms: {create() {}, onAlarm: event()},
};
const context = vm.createContext({
  chrome, WebSocket: Socket, URL, TextEncoder, crypto: webcrypto,
  console: {log() {}, warn() {}}, setTimeout() {}, clearTimeout() {},
});
const {playback, sensors, requests} = require('./tests/extension_worker.cjs').loadWorker(context);
// The startup setup reminder is separate from replaying playback/fetch sensors.
const actionInjections = () => injections.filter(call => !call.files?.includes('music_setup.js'));
(async () => {
  if (scenario === 'refresh_views') {
    await playback.refreshView('PL_edited');
    assert.deepEqual(sends, [
      {id: 1, type: 'refresh-view', playlist: 'PL_edited'},
      {id: 2, type: 'refresh-view', playlist: 'PL_edited'},
    ]);
  } else if (scenario === 'install') {
    await Promise.all(runtime.onInstalled.listeners.map(fn => fn({reason: 'update'})));
    assert.equal(reloads.length, 0, 'store updates must not reload music tabs');
    for (const id of [1, 2]) {
      assert(injections.some(call => call.target.tabId === id && call.files?.includes('content.js')));
      assert(injections.some(call => call.target.tabId === id && call.files?.includes('nowplaying_main.js')));
    }
  } else if (scenario === 'user_tabs') {
    tabs = [
      {id: 3, url: 'http://127.0.0.1:8765/playlists/one'},
      {id: 4, url: 'http://localhost:8765/songs?filter=liked'},
    ];
    for (const tab of tabs) {
      await Promise.all(chrome.tabs.onUpdated.listeners.map(fn => fn(tab.id, {status: 'complete'}, tab)));
    }
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(removed.length, 0, 'user-opened views must stay open');
    assert.equal(activated.length, 0, 'loading an app tab must not steal focus');
  } else if (['audible', 'playing', 'paused', 'unknown', 'idle'].includes(scenario)) {
    const result = await sensors.reloadUnresponsive(1);
    assert.equal(result, scenario === 'idle');
    assert.deepEqual(reloads, scenario === 'idle' ? [1] : []);
  } else if (scenario === 'next_missing_receiver') {
    assert.equal((await playback.next('v')).ok,true);
    assert.equal(attempts,2,'retry only after a proven missing receiver');
    assert(actionInjections().length>0);
  } else if (['playpause_lost_reply', 'rating_lost_reply', 'next_lost_reply'].includes(scenario)) {
    await (scenario === 'playpause_lost_reply' ? playback.playPause()
      : scenario === 'next_lost_reply' ? playback.next('v') : playback.rate('like', 'v'));
    assert.equal(attempts, 1, 'a lost click reply must not replay the toggle');
    assert.equal(actionInjections().length, 0);
  } else {
    const result = await requests.handle({id: 7, method: 'POST',
      url: 'https://music.youtube.com/youtubei/v1/browse/edit_playlist', body: {}});
    assert.equal(attempts, ['missing_receiver', 'lost_retry_reply'].includes(scenario) ? 2 : 1);
    assert.equal(result.status, scenario === 'missing_receiver' ? 200 : 0);
    if (['lost_reply', 'empty_reply', 'lost_retry_reply'].includes(scenario)) {
      assert.match(JSON.parse(result.body).error, /may have completed/);
    }
    if (['lost_reply', 'empty_reply'].includes(scenario)) assert.equal(actionInjections().length, 0);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scenario", ["new", "loading", "timeout", "closed", "removed", "launch_first", "fetch_first"])
def test_concurrent_fetches_share_document_and_sensor_readiness(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const scenario = process.argv[1];
const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); },
  removeListener(fn) { this.listeners = this.listeners.filter(listener => listener !== fn); }});
const runtime = {onMessage: event(), onStartup: event(), onInstalled: event()};
const onUpdated = event(), messages = [], injections = [], timers = new Map();
let now = 0, serial = 0, created = 0, sensorReady = false;
let tabs = scenario === 'loading' ? [
  {id: 1, url: 'about:blank', pendingUrl: 'https://music.youtube.com/', status: 'loading'},
] : [];
class Socket { static OPEN = 1; static CLOSED = 3; constructor() { this.readyState = 0; } }
const chrome = {
  runtime,
  cookies: {get: async () => ({value: 'test-cookie'})},
  tabs: {
    query: async ({url} = {}) => Array.isArray(url) ? [] : tabs,
    create: async options => {
      created++;
      const tab = {id: 1, url: 'about:blank', pendingUrl: options.url, status: 'loading'};
      tabs.push(tab);
      return tab;
    },
    update: async (id, changes) => {
      const tab = tabs.find(tab => tab.id === id);
      if (changes.url) Object.assign(tab, {pendingUrl: changes.url, status: 'loading'});
      return tab;
    },
    get(id, callback) {
      const tab = tabs.find(tab => tab.id === id);
      if (callback) {
        if (scenario === 'closed') runtime.lastError = {message: 'No tab with id: ' + id};
        callback(tab);
        delete runtime.lastError;
      }
      return Promise.resolve(tab);
    },
    onUpdated, onRemoved: event(),
    sendMessage: async (id, msg) => {
      messages.push(msg.type);
      if (msg.type === 'sensor-ping') {
        if (!sensorReady) throw Error('Could not establish connection. Receiving end does not exist.');
        return {ok: true, receiverRevision: 3, mainVersion: 4, mainRevision: 4, mainAgeMs: 0};
      }
      assert.equal(msg.type, 'fetch');
      assert.equal(tabs[0].status, 'complete');
      assert.equal(sensorReady, true, 'fetch must wait for both document and content readiness');
      return {status: 200, body: '{}'};
    },
  },
  scripting: {executeScript: async call => { injections.push(call); return []; }},
  windows: {onRemoved: event()},
  alarms: {create() {}, onAlarm: event()},
};
const context = vm.createContext({
  chrome, WebSocket: Socket, URL, TextEncoder, crypto: webcrypto,
  console: {log() {}, warn() {}},
  Date: class extends Date { static now() { return now; } },
  setTimeout(fn, delay) { const id = ++serial; timers.set(id, {fn, delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
});
const {tabs: musicTabs, playback, requests} = require('./tests/extension_worker.cjs').loadWorker(context);
const initialListeners = onUpdated.listeners.length;
const flush = () => new Promise(resolve => setImmediate(resolve));
const frame = id => requests.handle({id, method: 'POST', body: {},
  url: 'https://music.youtube.com/youtubei/v1/browse/edit_playlist'});
const runTimer = async delay => {
  const entry = [...timers].find(([, timer]) => timer.delay === delay);
  assert(entry, 'expected readiness timer');
  timers.delete(entry[0]);
  now += delay;
  entry[1].fn();
  await flush();
};
(async () => {
  const launchUrl = 'https://music.youtube.com/watch?v=requested&list=PL_requested';
  let launch;
  if (scenario === 'launch_first') launch = playback.navigate(launchUrl);
  const first = musicTabs.ensure();
  if (scenario === 'fetch_first') launch = playback.navigate(launchUrl);
  // Requests arriving before the initial query resolves share the same operation too.
  assert.equal(musicTabs.ensure(), first);
  const responses = [frame(1)];
  await flush();
  if (launch) {
    await launch;
    playback.cancelRescue();
    assert.equal(tabs.length, 1, 'playback and fetch startup must share one tab');
    assert.equal(tabs[0].pendingUrl, launchUrl, 'the requested queue must win over fetch startup');
  }
  assert.equal(created, scenario === 'loading' ? 0 : 1);
  if (scenario === 'closed') {
    assert.equal(await first, null);
    assert.equal((await responses[0]).status, 0);
    assert.equal(onUpdated.listeners.length, initialListeners);
    assert.equal(timers.size, 0);
    return;
  }
  assert.equal(musicTabs.ensure(), first, 'visible loading tab cannot bypass the shared wait');
  responses.push(frame(2), frame(3));
  await flush();
  assert.equal(messages.filter(type => type === 'fetch').length, 0);
  assert.equal(injections.filter(call => call.files).length, 0, 'do not inject into an uncommitted document');
  if (scenario === 'removed') {
    tabs = [];
    for (const listener of [...chrome.tabs.onRemoved.listeners]) listener(1);
    assert.equal(await first, null);
    assert((await Promise.all(responses)).every(reply => reply.status === 0));
    assert.equal(onUpdated.listeners.length, initialListeners);
    assert.equal(chrome.tabs.onRemoved.listeners.length, 1, 'remove the temporary removal waiter');
    assert.equal(timers.size, 0, 'tab removal must clear the document timeout immediately');
    return;
  }
  if (scenario === 'timeout') {
    await runTimer(15000);
    assert.equal(await first, null);
    assert((await Promise.all(responses)).every(reply => reply.status === 0));
    assert.equal(onUpdated.listeners.length, initialListeners);
    assert.equal(messages.filter(type => type === 'fetch').length, 0);
    return;
  }
  const tab = tabs[0];
  Object.assign(tab, {url: tab.pendingUrl, status: 'complete'});
  delete tab.pendingUrl;
  // Deliver the load event to the startup waiter. Sensor-health probes are covered separately.
  for (const listener of [...onUpdated.listeners].slice(initialListeners)) {
    listener(1, {status: 'complete'}, tab);
  }
  await flush();
  assert(injections.some(call => call.files?.includes('content.js')));
  assert.equal(messages.filter(type => type === 'fetch').length, 0, 'loaded alone is insufficient');
  sensorReady = true;
  await runTimer(300);
  assert.equal((await first).id, 1);
  const replies = await Promise.all(responses);
  assert.deepEqual(replies.map(reply => [reply.id, reply.status]), [[1, 200], [2, 200], [3, 200]]);
  assert.equal(messages.filter(type => type === 'fetch').length, 3, 'one delivery per request');
  assert(!messages.includes('resync-now'), 'readiness probes must not reset play deduplication');
  assert.equal(onUpdated.listeners.length, initialListeners);
  assert.equal(timers.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
