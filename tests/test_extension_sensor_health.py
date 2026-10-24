"""Health requires both sensor worlds; fetch readiness requires only the isolated receiver."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "fresh", "missing_main", "old_version", "stale", "missing_receiver", "partial_injection",
    "negative_age", "invalid_age", "infinite_age", "missing_observer", "old_main_revision", "old_receiver",
    "old_receiver_revision",
])
def test_health_repairs_main_sensor_even_when_content_receiver_answers(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise extension sensor recovery")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
const event = () => ({addListener() {}, removeListener() {}});
const injections = [], probes = [], frames = [], sockets = [], reloads = [];
let contentReady = scenario !== 'missing_receiver';
let receiverRevision = scenario === 'old_receiver' ? undefined : scenario === 'old_receiver_revision' ? 4 : 5;
let mainRevision = scenario === 'old_main_revision' ? 5 : 6;
let mainVersion = scenario === 'old_version' ? 3
  : ['missing_main', 'partial_injection', 'missing_receiver'].includes(scenario) ? null : 4;
let mainAgeMs = scenario === 'stale' ? 20000 : mainVersion === null ? null : 0;
if (scenario === 'negative_age') mainAgeMs = -1;
if (scenario === 'invalid_age') mainAgeMs = '0';
if (scenario === 'infinite_age') mainAgeMs = Infinity;
if (scenario === 'missing_observer') mainVersion = null;
class Socket {
  static OPEN = 1; static CLOSED = 3;
  constructor() { this.readyState = 0; sockets.push(this); }
  send(frame) { frames.push(JSON.parse(frame)); }
}
const tab = {id: 1, url: 'https://music.youtube.com/', status: 'complete', audible: true};
const chrome = {
  runtime: {onMessage: event(), onStartup: event(), onInstalled: event()},
  tabs: {
    query: async () => [tab], get: async () => tab,
    onUpdated: event(), onRemoved: event(), reload: async id => reloads.push(id),
    sendMessage: async (id, msg) => {
      assert.equal(id, 1);
      assert.equal(msg.type, 'sensor-ping');
      probes.push(msg);
      if (!contentReady) throw Error('Could not establish connection. Receiving end does not exist.');
      return {ok: true, mainVersion, mainRevision, mainAgeMs, receiverRevision,
        capture: {metadataVerified: false, accountKnown: true, barHasVideo: false}};
    },
  },
  scripting: {executeScript: async call => {
    injections.push(call);
    if (call.files.includes('content.js')) {
      contentReady = true;
      if (scenario !== 'old_receiver') receiverRevision = 5;
    }
    if (call.files.includes('nowplaying_main.js')) {
      if (scenario === 'partial_injection') throw Error('MAIN injection failed');
      mainVersion = 4;
      mainRevision = 6;
      mainAgeMs = 0;
    }
    if (scenario === 'missing_observer' && call.files.includes('observer_main.js'))
      throw Error('observer injection failed');
    return [];
  }},
  windows: {onRemoved: event()}, alarms: {create() {}, onAlarm: event()},
};
const context = vm.createContext({chrome, WebSocket: Socket, URL,
  console: {log() {}, warn() {}}, setTimeout() {}, clearTimeout() {}});
const {sensors} = require('./tests/extension_worker.cjs').loadWorker(context);
sockets[0].readyState = Socket.OPEN;
(async () => {
  if (contentReady) {
    assert.equal(await sensors.pingContent(1), true,
      'missing MAIN must not block the independent fetch receiver');
    assert.equal(probes.at(-1).main, false);
  }
  const report = await sensors.probe();
  const failed = ['partial_injection', 'missing_observer', 'old_receiver'].includes(scenario);
  assert.equal(report.healthy, !failed);
  assert.equal(report.respondingTabs, failed ? 0 : 1);
  assert.equal(report.reinjectedTabs, scenario === 'fresh' || ['partial_injection', 'missing_observer'].includes(scenario) ? 0 : 1);
  assert.equal(frames.at(-1).type, 'sensor-health');
  assert.equal(frames.at(-1).healthy, report.healthy);
  assert.equal(report.versions[0].mainRevision, 6);
  assert.equal(report.versions[0].receiverRevision, scenario === 'old_receiver' ? null : 5);
  assert.equal(report.versions[0].capture.metadataVerified, false,
    'healthy transport must not hide an ineligible exact-play producer');
  assert.equal(probes.at(-1).main, true);
  if (scenario === 'fresh') assert.equal(injections.filter(call => !call.files.includes('music_setup.js')).length, 0);
  else assert(injections.some(call => call.world === 'MAIN' && call.files.includes('nowplaying_main.js')));
  if (scenario === 'partial_injection') {
    assert.match(report.error, /MAIN injection failed/);
    assert.match(report.error, /MAIN sensor/);
  }
  if (scenario === 'old_receiver') assert.match(report.error, /Outdated rating receiver.*reload/);
  assert.equal(reloads.length, 0, 'repair must never reload audible playback');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
