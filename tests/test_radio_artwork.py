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
  state: {active: true, playlist_id: playlistId},
  drawingSpace: {width: 1200, historyHeight: 340, gapHeight: 112},
  queueCenters: {}, connectedSlot: 0,
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
first.state = {active: true, playlist_id: 'two'};
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

first.state = {active: false};
first.syncArtworkSession();
assert.equal(first.artworkPhase, 0);
assert.equal(storage.has('radio-artwork'), false, 'ending a session clears its saved drawing');
first.state = {active: true, playlist_id: 'two'};
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
