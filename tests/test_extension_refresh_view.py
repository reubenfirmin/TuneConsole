"""#95: playlist changes received during playback must remain queued until a safe refresh."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("path", ["/watch?v=v1&list=PL1", "/browse/VLPL1"])
@pytest.mark.parametrize("scenario", ["playing", "paused", "no_video", "ended", "navigated", "unrelated",
                                      "reload_failure"])
def test_playlist_refresh_reconciles_without_interrupting_playback(path, scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension content script")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const [path, scenario] = process.argv.slice(1);
const intervals = [], listeners = {};
let reloads = 0, reloadFails = scenario === 'reload_failure';
const video = {paused: ['paused', 'reload_failure'].includes(scenario), ended: false};
const window = {addEventListener(type, fn) { listeners[type] = fn; }};
const location = {
  href: 'https://music.youtube.com' + path,
  reload() { if (reloadFails) throw Error('temporary reload failure'); reloads += 1; },
};
const context = {
  window, location, URL,
  document: {
    addEventListener() {},
    querySelector(selector) { return selector === 'video' && scenario !== 'no_video' ? video : null; },
  },
  chrome: {runtime: {
    onMessage: {addListener(fn) { listeners.runtime = fn; }},
    sendMessage() {},
  }},
  setInterval(fn) { intervals.push(fn); },
  Date: {now() { return 10000; }},
  console: {log() {}, warn() {}},
};
vm.runInNewContext(fs.readFileSync('extension/content.js', 'utf8'), context);
const request = playlist => listeners.runtime({type: 'refresh-view', playlist}, {}, () => {});
const tick = () => intervals.forEach(fn => fn());
if (scenario === 'unrelated') {
  request('OTHER');
  video.paused = true;
  tick();
  assert.equal(reloads, 0, 'an unrelated playlist must never reload this tab');
} else if (['paused', 'no_video'].includes(scenario)) {
  request('PL1');
  assert.equal(reloads, 1, 'a safe matching tab should refresh immediately');
  tick();
  assert.equal(reloads, 1, 'a completed refresh must not remain queued');
} else if (scenario === 'reload_failure') {
  request('PL1');
  assert.equal(reloads, 0);
  reloadFails = false;
  tick();
  assert.equal(reloads, 1, 'a failed reload must retain the pending change');
} else {
  request('PL1');
  request('PL1');
  tick();
  assert.equal(reloads, 0, 'playlist edits must never interrupt active playback');
  if (scenario === 'navigated') {
    location.href = 'https://music.youtube.com/watch?v=v2&list=OTHER';
    tick();
    video.paused = true;
    tick();
    assert.equal(reloads, 0, 'a queued update cannot reload a different playlist');
    location.href = 'https://music.youtube.com' + path;
    tick();
    assert.equal(reloads, 0, 'navigating away should retire the old view update');
  } else {
    // The MAIN-world sensor may still be reporting; that must not starve deferred refreshes.
    listeners.message({source: window, data: {__tcNow: {title: 'Song', videoId: 'v1', paused: false}}});
    if (scenario === 'ended') video.ended = true;
    else video.paused = true;
    tick();
    assert.equal(reloads, 1, 'the original request must reconcile without a second bridge message');
    tick();
    assert.equal(reloads, 1, 'repeated requests should coalesce into one pending refresh');
  }
}
"""
    result = subprocess.run([node, "-e", script, path, scenario], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
