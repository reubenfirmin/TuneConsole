"""Health requires both sensor worlds; fetch readiness requires only the isolated receiver."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "fresh", "missing_main", "old_version", "stale", "missing_receiver", "partial_injection",
    "negative_age", "invalid_age", "infinite_age", "missing_observer", "old_main_revision", "old_receiver",
    "old_receiver_revision", "newer_main_revision", "newer_receiver_revision", "upgrade_unavailable",
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
let receiverRevision = scenario === 'old_receiver' ? undefined : scenario === 'old_receiver_revision' ? 6 : 9;
let mainRevision = scenario === 'old_main_revision' ? 7 : 9;
if (scenario === 'newer_main_revision') mainRevision = 10;
if (scenario === 'newer_receiver_revision') receiverRevision = 10;
if (scenario === 'upgrade_unavailable') receiverRevision = 6;
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
    if (scenario === 'upgrade_unavailable') throw Error('Could not inject an update');
    if (call.files.includes('content.js')) {
      contentReady = true;
      if (scenario !== 'old_receiver') receiverRevision = 9;
    }
    if (call.files.includes('nowplaying_main.js')) {
      if (scenario === 'partial_injection') throw Error('MAIN injection failed');
      mainVersion = 4;
      mainRevision = 9;
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
  const failed = ['partial_injection', 'missing_observer'].includes(scenario);
  const current = ['fresh', 'newer_main_revision', 'newer_receiver_revision'].includes(scenario);
  assert.equal(report.healthy, !failed);
  assert.equal(report.respondingTabs, failed ? 0 : 1);
  assert.equal(report.reinjectedTabs, current || failed || scenario === 'upgrade_unavailable' ? 0 : 1);
  assert.equal(frames.at(-1).type, 'sensor-health');
  assert.equal(frames.at(-1).healthy, report.healthy);
  assert.equal(report.versions[0].mainRevision, scenario === 'newer_main_revision' ? 10 : 9);
  assert.equal(report.versions[0].receiverRevision,
    scenario === 'old_receiver' ? null : scenario === 'newer_receiver_revision' ? 10 :
      scenario === 'upgrade_unavailable' ? 6 : 9);
  assert.equal(report.versions[0].capture.metadataVerified, false,
    'healthy transport must not hide an ineligible exact-play producer');
  assert.equal(probes.at(-1).main, true);
  if (current) assert.equal(injections.filter(call => !call.files.includes('music_setup.js')).length, 0);
  else if (scenario !== 'upgrade_unavailable')
    assert(injections.some(call => call.world === 'MAIN' && call.files.includes('nowplaying_main.js')));
  if (scenario === 'partial_injection') {
    assert.match(report.error, /MAIN injection failed/);
    assert.match(report.error, /MAIN sensor/);
  }
  if (scenario === 'old_receiver' || scenario === 'upgrade_unavailable') {
    assert.equal(report.error, '', 'a legacy rating receiver does not disconnect playback');
    const rating = await sensors.ping(1, true, {nativeRating: true});
    assert.equal(rating.ok, false, 'legacy receivers cannot cancel delayed rating clicks');
    assert.equal(rating.outdatedReceiver, true);
  }
  assert.equal(reloads.length, 0, 'repair must never reload audible playback');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scenario", [
    "legacy_bootstrap", "missing_receiver_option", "older_expectations", "legacy_receiver",
    "legacy_main", "older_main", "previous_release", "newer_revisions", "wrong_protocol", "new_protocol", "stale",
])
def test_sensor_compatibility_preserves_legacy_calls_and_rating_requirements(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise extension compatibility")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const scenario = process.argv[1];
const reply = {ok: true, receiverRevision: 8, mainVersion: 4, mainRevision: 9, mainAgeMs: 0};
if (scenario === 'legacy_receiver') delete reply.receiverRevision;
if (scenario === 'legacy_main') delete reply.mainRevision;
if (scenario === 'older_main') reply.mainRevision = 7;
if (scenario === 'previous_release') { reply.receiverRevision = 7; reply.mainRevision = 8; }
if (scenario === 'newer_revisions') { reply.receiverRevision = 9; reply.mainRevision = 10; }
if (scenario === 'wrong_protocol') reply.mainVersion = 3;
if (scenario === 'new_protocol') reply.mainVersion = 5;
if (scenario === 'stale') reply.mainAgeMs = 20000;
const context = vm.createContext({chrome: {tabs: {sendMessage: async () => reply}}});
for (const file of ['config', 'sensors'])
  vm.runInContext(fs.readFileSync(`extension/worker/${file}.js`, 'utf8'), context);
const options = {version: 4, warn() {}, send() {}, suppressBeforeUnload() {}};
if (scenario !== 'legacy_bootstrap') options.revision = scenario === 'older_expectations' ? 8 : 9;
if (!['legacy_bootstrap', 'missing_receiver_option'].includes(scenario))
  options.receiverRevision = scenario === 'older_expectations' ? 7 : 8;
const sensors = vm.runInContext('createSensors', context)(options);
(async () => {
  assert.equal(await sensors.pingContent(1), true, 'fetch readiness is independent of MAIN');
  const health = await sensors.ping(1, true, {nativeRating: false});
  assert.equal(health.ok, !['wrong_protocol', 'new_protocol', 'stale'].includes(scenario));
  if (scenario === 'previous_release') assert.equal(health.needsUpgrade, true);
  const rating = await sensors.ping(1, true, {nativeRating: true});
  assert.equal(rating.ok,
    !['legacy_receiver', 'legacy_main', 'older_main', 'wrong_protocol', 'new_protocol', 'stale'].includes(scenario));
  assert.equal((await sensors.ping(1)).ok, rating.ok,
    'older rating modules retain strict readiness without the new options argument');
  if (scenario === 'legacy_receiver') assert.equal(rating.outdatedReceiver, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
