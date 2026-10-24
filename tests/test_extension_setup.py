"""Setup messaging exposes availability only to the Music content script and toolbar panel."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_setup_status_is_scoped_and_contains_no_listening_data():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for extension checks")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let connected = false;
let installType = 'normal';
const sent = [], installed = [], opened = [], updated = [];
const tabs = [
  {id: 1, url: 'https://music.youtube.com/watch?v=private'},
  {id: 2, url: 'https://music.youtube.com.evil.example/'},
  {id: 3, url: 'https://tuneconsole.com/'},
];
const context = vm.createContext({URL, setTimeout, clearTimeout, chrome: {
  runtime: {getURL: path => 'chrome-extension://test/' + path},
  management: {getSelf: async () => ({installType})},
  scripting: {executeScript: async call => installed.push(call)},
  tabs: {
    create: async call => opened.push(call),
    query: async () => tabs,
    update: async (id, props) => {
      updated.push({id, ...props});
      Object.assign(tabs.find(tab => tab.id === id), props);
    },
    sendMessage: async (id, msg) => sent.push({id, ...msg}),
  },
}});
vm.runInContext(fs.readFileSync('extension/worker/setup.js', 'utf8'), context);
const setup = vm.runInContext('createSetupIntegration', context)({
  musicOrigin: 'https://music.youtube.com', connected: () => connected,
});
function request(url) {
  let reply;
  setup.receive({type: 'music-setup-status'}, {url}, value => {reply = value;});
  return reply && JSON.parse(JSON.stringify(reply));
}
(async () => {
  await setup.publish();
  sent.length = 0;
  await setup.onInstalled({reason: 'update'});
  await setup.onInstalled({reason: 'chrome_update'});
  await setup.onInstalled();
  assert.deepEqual(opened, []);
  await setup.onInstalled({reason: 'install'});
  assert.deepEqual(JSON.parse(JSON.stringify(updated)),
    [{id: 3, active: true, url: 'https://tuneconsole.com/extension-setup'}]);
  installType = 'development';
  await setup.onInstalled({reason: 'update'});
  assert.equal(updated.length, 2, 'unpacked reloads must activate the post-install page');
  assert.deepEqual(JSON.parse(JSON.stringify(updated[1])), {id: 3, active: true},
    'do not reload setup or reset the user’s install choices');
  await setup.onInstalled({reason: 'chrome_update'});
  assert.equal(updated.length, 2, 'browser updates stay quiet even for unpacked extensions');
  await setup.openDestination();
  assert.equal(updated.length, 3, 'enable reuses the same post-install destination');
  assert.deepEqual(opened, [], 'an existing TuneConsole site tab is enough');
  await setup.install();
  assert.deepEqual(JSON.parse(JSON.stringify(installed)),
    [{target: {tabId: 1}, files: ['music_setup.js']}]);
  assert.deepEqual(request('https://music.youtube.com/'), {type: 'music-setup-status', connected: false});
  assert.equal(request('https://music.youtube.com.evil.example/'), undefined);
  assert.equal(request('https://tuneconsole.com/'), undefined);
  assert.equal(request('chrome-extension://other/setup.html'), undefined);
  assert.equal(request('chrome-extension://test/other.html'), undefined);
  assert.equal(request(undefined), undefined);
  assert.deepEqual(request('chrome-extension://test/setup.html'), {type: 'music-setup-status', connected: false});
  connected = true;
  await setup.publish();
  assert.deepEqual(sent, [{id: 1, type: 'music-setup-status', connected: true}]);
  assert.deepEqual(request('chrome-extension://test/setup.html'), {type: 'music-setup-status', connected: true});
  connected = false;
  await setup.publish();
  assert.deepEqual(sent.at(-1), {id: 1, type: 'music-setup-status', connected: false});
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_setup_reuses_and_focuses_site_tabs_without_duplicate_navigation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for extension checks")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('extension/worker/setup.js', 'utf8');
async function check(tabs, expected) {
  const opened = [], updated = [], focused = [];
  const context = vm.createContext({URL, setTimeout, clearTimeout, chrome: {
    tabs: {
      query: async () => tabs,
      create: async props => opened.push(props),
      update: async (id, props) => updated.push({id, ...props}),
    },
    windows: {update: async (id, props) => focused.push({id, ...props})},
  }});
  vm.runInContext(source, context);
  const setup = vm.runInContext('createSetupIntegration', context)({
    musicOrigin: 'https://music.youtube.com', connected: () => false,
  });
  await setup.publish();
  await Promise.all([setup.openDestination(), setup.openDestination(), setup.openDestination()]);
  const plain = value => JSON.parse(JSON.stringify(value));
  if (expected) {
    assert.deepEqual(plain(updated), [expected]);
    assert.deepEqual(plain(focused), [{id: 7, focused: true}]);
    assert.deepEqual(opened, []);
  } else {
    assert.deepEqual(plain(opened), [{url: 'https://tuneconsole.com/extension-setup'}]);
    assert.deepEqual(updated, []);
  }
}
(async () => {
  await check([
    {id: 1, windowId: 8, active: true, url: 'https://tuneconsole.com/'},
    {id: 2, windowId: 7, url: 'https://www.tuneconsole.com/extension-setup#container-updates'},
  ], {id: 2, active: true});
  await check([
    {id: 1, windowId: 8, url: 'https://www.tuneconsole.com/privacy.html'},
    {id: 2, windowId: 7, active: true, url: 'https://tuneconsole.com/#install'},
  ], {id: 2, active: true, url: 'https://tuneconsole.com/extension-setup'});
  await check([
    {id: 2, windowId: 7, url: 'about:blank', pendingUrl: 'https://tuneconsole.com/extension-setup'},
  ], {id: 2, active: true});
  await check([
    {id: 1, url: 'https://tuneconsole.com.evil.example/'},
    {id: 2, url: 'http://tuneconsole.com/'},
    {id: 3, url: 'https://tuneconsole.com/', pendingUrl: 'https://example.org/'},
  ], null);
  await check([], null);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("connection", ["connected", "connecting", "offline", "timeout"])
def test_install_checks_the_app_before_choosing_a_destination(connection):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for extension checks")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
let connected = scenario === 'connected', localOpened = 0, timeout;
const opened = [];
const context = vm.createContext({URL,
  setTimeout(fn) { timeout = fn; return 1; }, clearTimeout() {},
  chrome: {tabs: {query: async () => [], create: async call => opened.push(call)}},
});
vm.runInContext(fs.readFileSync('extension/worker/setup.js', 'utf8'), context);
const setup = vm.runInContext('createSetupIntegration', context)({
  musicOrigin: 'https://music.youtube.com', connected: () => connected,
  openPlayer: async () => { localOpened++; return {opened: true}; },
});
(async () => {
  const launch = setup.onInstalled({reason: 'install'});
  if (scenario !== 'connected') {
    assert.equal(opened.length, 0, 'do not show install instructions before checking the app');
    assert.equal(localOpened, 0);
    if (scenario === 'timeout') timeout();
    else { connected = scenario === 'connecting'; await setup.publish(); }
  }
  await launch;
  assert.equal(localOpened, ['connected', 'connecting'].includes(scenario) ? 1 : 0);
  assert.deepEqual(JSON.parse(JSON.stringify(opened)), localOpened ? []
    : [{url: 'https://tuneconsole.com/extension-setup'}]);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script, connection], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
