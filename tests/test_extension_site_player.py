"""The public-site badge follows the real bridge socket and stays scoped to the public site."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_site_status_follows_socket_and_recovers_existing_tabs():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); }});
const runtime = {onMessage: event(), onStartup: event(), onInstalled: event()};
const messages = [], injected = [], styles = [], sockets = [];
const opened = [], activated = [], focused = [];
let appTabs = [];
let missingReceiver = true;
const siteTabs = [
  {id: 1, url: 'https://tuneconsole.com/'},
  {id: 2, url: 'https://www.tuneconsole.com/'},
  {id: 3, url: 'https://tuneconsole.com.evil.example/'},
];
class Socket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
  send() {}
}
const chrome = {
  runtime,
  storage: {session: {get: async () => ({})}},
  tabs: {
    query: async ({url}) => Array.isArray(url) ? siteTabs
      : url ? [] : [...siteTabs, ...appTabs],
    create: async tab => {
      opened.push({url: tab.url, active: tab.active});
      appTabs = [{id: 41, windowId: 7, url: tab.url}];
    },
    update: async (id, props) => activated.push({id, active: props.active}),
    sendMessage: async (id, msg) => {
      if (id === 2 && missingReceiver) { missingReceiver = false; throw Error('no receiver'); }
      messages.push({id, connected: msg.connected});
    },
    onUpdated: event(), onRemoved: event(),
  },
  scripting: {
    executeScript: async call => injected.push(call.target.tabId),
    insertCSS: async call => styles.push(call.target.tabId),
  },
  windows: {onRemoved: event(), update: async (id, props) => focused.push({id, focused: props.focused})},
  alarms: {create() {}, onAlarm: event()},
};
vm.runInNewContext(fs.readFileSync('extension/background.js', 'utf8'), {
  chrome, WebSocket: Socket, URL, console: {log() {}, warn() {}}, setTimeout() {}, clearTimeout() {},
});
function status(url) {
  let reply;
  for (const listener of runtime.onMessage.listeners) {
    listener({type: 'local-player-status'}, {url, tab: {id: 1, url}}, value => { reply = value; });
  }
  return reply;
}
function openPlayer() {
  return new Promise(resolve => {
    for (const listener of runtime.onMessage.listeners) {
      listener({type: 'open-local-player'}, {url: 'https://tuneconsole.com/', tab: {id: 1}}, resolve);
    }
  });
}
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  assert.equal(status('https://tuneconsole.com/').connected, false);
  assert.equal(status('https://www.tuneconsole.com/').connected, false);
  assert.equal(status('https://tuneconsole.com.evil.example/'), undefined);
  assert.equal(status('https://music.youtube.com/'), undefined);
  const socket = sockets[0];
  socket.readyState = Socket.OPEN;
  await socket.onopen();
  await flush();
  assert.equal(status('https://tuneconsole.com/').connected, true);
  assert.deepEqual(messages.filter(m => m.connected).map(m => m.id).sort(), [1, 2]);
  assert.deepEqual(injected, [2]);
  assert.deepEqual(styles, [2]);
  assert.equal((await openPlayer()).opened, true);
  assert.deepEqual(opened, [{url: 'http://127.0.0.1:8765/', active: true}]);
  assert.equal((await openPlayer()).opened, true);
  assert.equal(opened.length, 1);
  assert.deepEqual(activated, [{id: 41, active: true}]);
  assert.deepEqual(focused, [{id: 7, focused: true}]);
  for (const location of [
    {url: 'http://127.0.0.1:8765/playlists/PL123?sort=recent#tracks'},
    {url: 'http://localhost:8765/'},
    {url: 'http://localhost:8765/albums?filter=saved'},
    {url: 'about:blank', pendingUrl: 'http://localhost:8765/playlists'},
  ]) {
    appTabs = [{id: 42, windowId: 8, ...location}];
    assert.equal((await openPlayer()).opened, true);
    assert.equal(opened.length, 1, 'reuse the existing local app tab');
    assert.deepEqual(activated.at(-1), {id: 42, active: true});
    assert.deepEqual(focused.at(-1), {id: 8, focused: true});
  }
  appTabs = [
    {id: 43, url: 'http://localhost:9000/'},
    {id: 44, url: 'https://localhost:8765/'},
    {id: 45, url: 'http://localhost.evil.example:8765/'},
    {id: 46, url: 'http://127.0.0.1:8765/', pendingUrl: 'https://example.org/'},
  ];
  const replies = await Promise.all([openPlayer(), openPlayer()]);
  assert(replies.every(reply => reply.opened));
  assert.equal(opened.length, 2, 'only create one tab, and ignore unrelated local services');
  messages.length = 0;
  socket.readyState = Socket.CLOSED;
  socket.onclose();
  await flush();
  assert.equal(status('https://tuneconsole.com/').connected, false);
  assert.equal((await openPlayer()).opened, false);
  assert.equal(opened.length, 2);
  assert.deepEqual(messages, [{id: 1, connected: false}, {id: 2, connected: false}]);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "-e", script], cwd=root, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
