"""Radio landscapes start composed, diverge between sessions, and resume exactly."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_artwork_session_identity_randomness_and_persistence():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise radio artwork")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const storage = new Map([['radio-artwork-phase', '15']]);
const math = Object.create(Math);
let random = .1;
math.random = () => random;
const context = vm.createContext({Math: math,
  cancelAnimationFrame() {},
  sessionStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  },
});
vm.runInContext(fs.readFileSync('src/yt_playlist/web/static/radio-artwork.js', 'utf8'), context);
const model = playlistId => Object.assign(vm.runInContext('radioArtwork()', context), {
  artworkView: {playlistId, connectedSlot: 0, directionCount: 4},
  drawingSpace: {width: 1200, historyHeight: 340, gapHeight: 112},
  queueCenters: {},
});
const paths = radio => Array.from({length: 14}, (_, i) => radio.landscapeStyle(i));
const first = model('one');
first.syncArtworkSession();
assert.equal(first.artworkPhase, 0, 'legacy global progress cannot scramble a new session');
const composed = paths(first);
first.artworkPhase = Math.PI * 5;
const evolved = paths(first), seed = first.artworkSeed;
assert.notDeepEqual(evolved, composed);
first.pauseArtworkMotion();
const resumed = model('one');
resumed.syncArtworkSession();
assert.equal(resumed.artworkPhase, first.artworkPhase);
assert.equal(resumed.artworkSeed, seed);
assert.deepEqual(paths(resumed), evolved, 'navigation restores the same geometry and rhythms');
resumed.syncArtworkSession();
assert.deepEqual(paths(resumed), evolved, 'status refreshes do not choose another motion');

random = .8;
first.artworkView.playlistId = 'two';
first.syncArtworkSession();
assert.equal(first.artworkPhase, 0);
assert.notEqual(first.artworkSeed, seed);
assert.deepEqual(paths(first), composed, 'a new session starts with the composed layout');
first.artworkPhase = resumed.artworkPhase;
assert.notDeepEqual(paths(first), evolved, 'sessions reach different shapes after equal listening time');
const before = paths(first);
first.artworkPhase += .00001;
const after = paths(first);
const numbers = paths => paths.flatMap(path => path.match(/-?\d+(?:\.\d+)?/g).map(Number));
assert.ok(numbers(before).every((value, i) => Math.abs(value - numbers(after)[i]) < .1),
  'randomness changes the trajectory without frame-to-frame jumps');

first.artworkView.playlistId = null;
first.syncArtworkSession();
assert.equal(first.artworkPhase, 0);
assert.equal(storage.has('radio-artwork'), false, 'ending a session clears its saved drawing');
first.artworkView.playlistId = 'two';
first.syncArtworkSession();
assert.equal(first.artworkPhase, 0);

for (const saved of ['null', '{bad', JSON.stringify({playlistId: 'bad', phase: -1, seed: 1}),
  JSON.stringify({playlistId: 'bad', phase: 2, seed: 'invalid'}),
  JSON.stringify({playlistId: 'another', phase: 12, seed: 1})]) {
  storage.set('radio-artwork', saved);
  const fresh = model('bad');
  fresh.syncArtworkSession();
  assert.equal(fresh.artworkPhase, 0);
  assert.deepEqual(paths(fresh), composed);
}
context.sessionStorage.getItem = () => { throw new Error('storage unavailable'); };
context.sessionStorage.setItem = context.sessionStorage.getItem;
context.sessionStorage.removeItem = context.sessionStorage.getItem;
const privateSession = model('private');
privateSession.syncArtworkSession();
privateSession.pauseArtworkMotion();
assert.deepEqual(paths(privateSession), composed, 'blocked storage still gives an orderly live drawing');
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_artwork_owns_motion_and_geometry_without_session_payloads():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise radio artwork")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const document = new EventTarget(), window = new EventTarget();
const frames = new Map(), storage = new Map();
let now = 0, frameId = 0;
document.hidden = false;
const context = vm.createContext({document, window, performance: {now: () => now},
  requestAnimationFrame: callback => { frames.set(++frameId, callback); return frameId; },
  cancelAnimationFrame: id => frames.delete(id),
  sessionStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  },
  fetch() { throw new Error('Artwork cannot issue requests'); },
});
for (const file of ['radio-artwork.js', 'radio.js'])
  vm.runInContext(fs.readFileSync('src/yt_playlist/web/static/' + file, 'utf8'), context);
const tick = elapsed => {
  now += elapsed;
  const pending = [...frames.values()];
  frames.clear();
  pending.forEach(callback => callback(now));
};
const artwork = vm.runInContext('radioArtwork()', context);
artwork.artworkView = {playlistId: 'one', visible: true, moving: true,
  connectedSlot: 0, directionCount: 4};
Object.defineProperty(artwork, 'state', {get() { throw new Error('Session payload leaked'); }});
artwork.initArtwork();
artwork.syncArtworkMotion();
assert.equal(frames.size, 1);
artwork.syncArtworkMotion();
assert.equal(frames.size, 1, 'status updates cannot duplicate the motion loop');
tick(120);
assert.ok(artwork.artworkPhase > 0);
const phase = artwork.artworkPhase;
document.hidden = true;
document.dispatchEvent(new Event('visibilitychange'));
assert.equal(frames.size, 0);
assert.equal(artwork.artworkRunning, false);
tick(30 * 60_000);
assert.equal(artwork.artworkPhase, phase);
document.hidden = false;
document.dispatchEvent(new Event('visibilitychange'));
tick(120);
assert.ok(artwork.artworkPhase > phase && artwork.artworkPhase < phase * 3,
  'hidden time is not replayed when the document returns');
window.dispatchEvent(new Event('pagehide'));
assert.equal(frames.size, 0);
window.dispatchEvent(new Event('pageshow'));
assert.equal(frames.size, 1);
artwork.artworkView.moving = false;
tick(120);
assert.equal(frames.size, 0, 'the view can pause drawing without a session payload');
artwork.artworkView.moving = true;
artwork.syncArtworkMotion();
const savedPhase = artwork.artworkPhase;
artwork.destroyArtwork();
assert.equal(frames.size, 0);
assert.equal(JSON.parse(storage.get('radio-artwork')).phase, savedPhase);
for (const [target, event] of [[document, 'visibilitychange'], [window, 'pageshow']])
  target.dispatchEvent(new Event(event));
assert.equal(frames.size, 0, 'fragment cleanup detaches listeners that could restart drawing');

// Compose through the real adapter: object spread would freeze these getters
// at the initial dimensions, even though the drawing fields keep changing.
const radio = vm.runInContext('radioHome()', context);
assert.equal(typeof Object.getOwnPropertyDescriptor(radio, 'drawingClearance').get, 'function');
const initial = [radio.engravingTransform, radio.landscapeToJunction, radio.drawingClearance];
radio.drawingSpace = {width: 390, historyHeight: 480, gapHeight: 130};
for (const [index, value] of [radio.engravingTransform, radio.landscapeToJunction, radio.drawingClearance].entries())
  assert.notEqual(value, initial[index], 'computed geometry follows viewport measurements');
const clearance = radio.junctionClearance;
radio.junctionHub = {...radio.junctionHub, x: 280, rx: 65};
assert.notEqual(radio.junctionClearance, clearance);
radio.state = {active: true, playlist_id: 'two', directions: [{slot: 2, active: true}]};
radio.visible = true;
assert.equal(radio.artworkView.playlistId, 'two');
assert.equal(radio.artworkView.connectedSlot, 2);
assert.equal(radio.artworkView.directionCount, 1);
assert.equal(radio.artworkView.moving, true);
radio.hoveredSlot = 1;
assert.equal(radio.artworkView.connectedSlot, 1);
for (const detail of [false, true])
  assert.doesNotMatch(radio.junctionStyle(1, detail), /NaN|Infinity/);
radio.destroyed = true;
assert.equal(radio.artworkView.moving, false);
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_record_orbits_fit_available_space_and_resume_the_same_motion():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to exercise radio artwork')
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const storage=new Map();
const context=vm.createContext({cancelAnimationFrame(){},sessionStorage:{
  getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}});
vm.runInContext(fs.readFileSync('src/yt_playlist/web/static/radio-artwork.js','utf8'),context);
const artwork=vm.runInContext('radioArtwork()',context);
artwork.artworkView={playlistId:'one'}; artwork.syncArtworkSession();
for(const [width,height] of [[1440,250],[1792,450],[390,140],[390,480],[120,60]]) {
  artwork.drawingSpace={width,historyHeight:height,gapHeight:112};
  artwork.artworkPhase=0;
  const starting=artwork.recordEchoes;
  assert(starting.length<=24,'the drawing has a fixed upper cost');
  if(width>=390) assert(starting.length>0,'available space gains smaller records');
  for(const phase of [0,.2,1,2,7,1000]) {
    artwork.artworkPhase=phase;
    const copies=artwork.recordEchoes;
    assert.equal(copies.length,starting.length,'orbiting records do not appear or disappear');
    for(const [index,copy] of copies.entries()) {
      assert(copy.x-copy.r>=0 && copy.x+copy.r<=width);
      assert(copy.y-copy.r>=0 && copy.y+copy.r<=height);
      assert(!/NaN|Infinity/.test(copy.transform));
      for(const other of copies.slice(index+1))
        assert(Math.hypot(copy.x-other.x,copy.y-other.y)>copy.r+other.r,'records remain separated');
    }
  }
  artwork.artworkPhase=2;
  const moved=artwork.recordEchoes;
  if(width>=390) assert(moved.some((copy,i)=>Math.hypot(copy.x-starting[i].x,copy.y-starting[i].y)>1),
    'smaller records orbit the main record');
  artwork.artworkPhase+=.00001;
  assert(artwork.recordEchoes.every((copy,i)=>Math.hypot(copy.x-moved[i].x,copy.y-moved[i].y)<.01),
    'orbital movement stays continuous');
}
artwork.drawingSpace={width:1440,historyHeight:250,gapHeight:112};
artwork.artworkPhase=0;
const composed=[artwork.orbitTransform(0),artwork.orbitTransform(1)];
artwork.artworkPhase=2;
const shifted=[artwork.orbitTransform(0),artwork.orbitTransform(1)], copies=artwork.recordEchoes;
assert.notDeepEqual(shifted,composed,'both rings change their position and plane');
artwork.pauseArtworkMotion();
const resumed=vm.runInContext('radioArtwork()',context);
resumed.artworkView={playlistId:'one'};
resumed.drawingSpace=artwork.drawingSpace;
resumed.syncArtworkSession();
assert.deepEqual(resumed.recordEchoes,copies,'navigation restores the same orbital positions');
assert.deepEqual([resumed.orbitTransform(0),resumed.orbitTransform(1)],shifted);
artwork.artworkPhase+=.00001;
const numbers=value=>value.match(/-?\d+(?:\.\d+)?/g).map(Number);
for(const [index,before] of shifted.entries()) {
  const after=numbers(artwork.orbitTransform(index));
  assert(numbers(before).every((value,i)=>Math.abs(value-after[i])<.01),'ring movement stays continuous');
}
"""
    result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
