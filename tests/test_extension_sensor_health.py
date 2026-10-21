"""Health requires both sensor worlds; fetch readiness requires only the isolated receiver."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "fresh", "missing_main", "old_version", "stale", "missing_receiver", "partial_injection",
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
let mainVersion = scenario === 'old_version' ? 3
  : ['missing_main', 'partial_injection', 'missing_receiver'].includes(scenario) ? null : 4;
let mainAgeMs = scenario === 'stale' ? 20000 : mainVersion === null ? null : 0;
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
      return {ok: true, mainVersion, mainAgeMs};
    },
  },
  scripting: {executeScript: async call => {
    injections.push(call);
    if (call.files.includes('content.js')) contentReady = true;
    if (call.files.includes('nowplaying_main.js')) {
      if (scenario === 'partial_injection') throw Error('MAIN injection failed');
      mainVersion = 4;
      mainAgeMs = 0;
    }
    return [];
  }},
  windows: {onRemoved: event()}, alarms: {create() {}, onAlarm: event()},
};
const context = vm.createContext({chrome, WebSocket: Socket, URL,
  console: {log() {}, warn() {}}, setTimeout() {}, clearTimeout() {}});
vm.runInContext(fs.readFileSync('extension/background.js', 'utf8'), context);
sockets[0].readyState = Socket.OPEN;
(async () => {
  if (contentReady) {
    assert.equal(await context.pingContentScript(1), true,
      'missing MAIN must not block the independent fetch receiver');
    assert.equal(probes.at(-1).main, false);
  }
  const report = await context.probeSensorHealth();
  assert.equal(report.healthy, scenario !== 'partial_injection');
  assert.equal(report.respondingTabs, scenario === 'partial_injection' ? 0 : 1);
  assert.equal(report.reinjectedTabs, ['fresh', 'partial_injection'].includes(scenario) ? 0 : 1);
  assert.equal(frames.at(-1).type, 'sensor-health');
  assert.equal(frames.at(-1).healthy, report.healthy);
  assert.equal(probes.at(-1).main, true);
  if (scenario === 'fresh') assert.equal(injections.length, 0);
  else assert(injections.some(call => call.world === 'MAIN' && call.files.includes('nowplaying_main.js')));
  if (scenario === 'partial_injection') {
    assert.match(report.error, /MAIN injection failed/);
    assert.match(report.error, /MAIN sensor/);
  }
  assert.equal(reloads.length, 0, 'repair must never reload audible playback');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
