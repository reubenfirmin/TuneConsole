"""Playback interpolation uses the shared status owner, without another HTTP polling loop."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_shared_progress_reconciles_seeks_pause_navigation_expiry_and_invalid_durations():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to exercise playback progress')
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const root = 'src/yt_playlist/web/static/';
const window = new EventTarget(), document = new EventTarget();
let now = 0, next = 0, calls = 0;
const frames = new Map(), timers = new Map(), deadlines = new Map();
window.performance = {now: () => now};
document.readyState = 'loading';
document.getElementById = () => null;
document.querySelector = () => null;
let status;
const context = vm.createContext({window, document, CustomEvent, AbortController,
  location: new URL('http://localhost/artists'), URL,
  requestAnimationFrame: fn => { frames.set(++next, fn); return next; },
  cancelAnimationFrame: id => frames.delete(id),
  setInterval: fn => { timers.set(++next, fn); return next; },
  clearInterval: id => timers.delete(id),
  setTimeout: (fn,ms) => { deadlines.set(++next, {fn,at:now+ms}); return next; },
  clearTimeout: id => deadlines.delete(id),
  fetch: async () => { calls++; return {ok: true, json: async () => status}; },
});
vm.runInContext(fs.readFileSync(root+'playback-status.js','utf8'),context);
vm.runInContext(fs.readFileSync(root+'player-card.js','utf8'),context);
const owner = window.playbackStatus;
const dock = vm.runInContext('playerStatus()',context);
window.addEventListener('bridge-status',e => dock.apply(e.detail));
window.addEventListener('playback-progress',e => dock.applyProgress(e.detail));
const tick = ms => {
  now += ms;
  const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn(now));
  for (const [id,deadline] of [...deadlines]) {
    if (deadline.at <= now) { deadlines.delete(id); deadline.fn(); }
  }
};
async function snapshot(changes={}, connected=true) {
  status = {connected,now_playing: {video_id:'one',title:'Song',position:10,duration:200,
    paused:false,snapshot_age:0,...changes}};
  await owner.refresh();
}
(async () => {
  await snapshot({snapshot_age:2});
  assert.equal(dock.timing.position,12,'account for an already aged sensor snapshot');
  tick(500); assert.equal(dock.timing.position,12.5);
  assert.equal(calls,1,'animation does not poll HTTP');
  const replacement = vm.runInContext('playerStatus()',context);
  replacement.init();
  assert.equal(replacement.timing.position,12.5,'navigation mounts from the current shared position');
  await owner.refresh(); // Finish the initial start refresh before publishing a seek.
  await snapshot({position:90});
  assert.equal(dock.timing.position,90,'forward seek replaces the anchor');
  await snapshot({position:4,paused:true});
  tick(500); assert.equal(dock.timing.position,4,'pause freezes progress');
  assert.equal(frames.size,0);
  tick(7600); assert.equal(dock.timing.position,null,'paused timing also expires during a hanging request');
  assert.equal(dock.timing.duration,null); assert.equal(deadlines.size,0);
  await snapshot({position:4}); tick(1000); assert.equal(dock.timing.position,5);
  await snapshot({video_id:'two',position:0,duration:60});
  assert.equal(dock.timing.position,0,'new upload does not borrow the previous position');
  tick(500); assert.equal(dock.timing.position,.5);
  await snapshot({video_id:'two',position:59.95,duration:60});
  tick(60); assert.equal(dock.timing.position,60); assert.equal(frames.size,0);
  for (const duration of [null,0,-1,'200',NaN,Infinity]) {
    await snapshot({duration});
    assert.equal(dock.timing.duration,null);
    tick(500); assert.equal(dock.timing.position,10.5,'elapsed time is useful without a total');
  }
  for (const position of [null,-1,'12',NaN,Infinity]) {
    await snapshot({position});
    assert.equal(dock.timing.position,null); assert.equal(frames.size,0);
  }
  await snapshot(); tick(8100);
  assert.equal(dock.timing.position,null,'a hanging request cannot animate expired playback');
  assert.equal(frames.size,0);
  await snapshot({position:30});
  assert.equal(dock.timing.position,30,'fresh reconnect restores authoritative position');
  await snapshot({},false); tick(1000);
  assert.equal(dock.timing.position,null); assert.equal(frames.size,0);
  status = {connected:true,now_playing:null};
  await owner.refresh(); tick(1000);
  assert.equal(dock.timing.position,null,'a removed track leaves a usable unknown timing object');
  assert.equal(dock.timing.duration,null); assert.equal(frames.size,0);
  await snapshot(); owner.stop(); tick(1000);
  assert.equal(dock.timing.position,null,'suspended pages cannot keep animating');
  assert.equal(frames.size,0); assert.equal(timers.size,0); assert.equal(deadlines.size,0);
  assert.equal(dock.timeLabel(null),'--:--'); assert.equal(dock.timeLabel(0),'0:00');
  assert.equal(dock.timeLabel(125.9),'2:05'); assert.equal(dock.timeLabel(3725),'1:02:05');
})().catch(error => { console.error(error); process.exitCode=1; });
"""
    result = subprocess.run([node, '-e', script],cwd=Path(__file__).resolve().parents[1],
                            capture_output=True,text=True,timeout=20)
    assert result.returncode==0, result.stdout+result.stderr
