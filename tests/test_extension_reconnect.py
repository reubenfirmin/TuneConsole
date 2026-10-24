"""Reconnect samples the real MAIN sensor instead of promoting stale DOM metadata."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario", ["transition", "conflicting_bar", "playing", "paused", "continuous_stream"])
def test_reconnect_preserves_main_identity_and_verification(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the extension sensor harness")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const scenario = process.argv[1], sent = [], queued = [], handlers = [], intervals = [];
let now = 100000;
const video = {paused: scenario === 'paused', currentTime: 20, duration: 200,
  muted:false, volume:1, addEventListener() {}, removeEventListener() {}};
let playerId = 'current', position = 20;
let bufferedDuration = 49.886619437641684;
if (scenario === 'continuous_stream') {video.currentTime=650; video.duration=665;}
let barId = scenario === 'transition' ? 'old' : 'current';
let imageId = ['transition','conflicting_bar'].includes(scenario) ? 'old' : 'current';
const window = {
  ytcfg: {get: key => key === 'LOGGED_IN' ? true : 'brand-A'},
  addEventListener(type, fn) { if (type === 'message') handlers.push(fn); },
  removeEventListener() {}, postMessage(msg) { queued.push(msg); },
};
const document = {addEventListener() {}, querySelector(selector) {
  if (selector === 'video') return video;
  if (selector === '#movie_player') return {
    getVideoData: () => ({video_id:playerId,title:'Current player song',author:'Uploader'}),
    getPlaylistId: () => 'PL_current',
    getCurrentTime: () => scenario === 'continuous_stream' ? position : video.currentTime,
    // Captured from live Music playback: this is a growing stream endpoint, not song length.
    getDuration: () => bufferedDuration,
  };
  if (selector === 'ytmusic-player-bar') return {data:{videoId:barId},getAttribute:()=>''};
  if (selector === '.title.ytmusic-player-bar') return {textContent:barId === 'old' ? 'Old song' : 'Current song'};
  if (selector === '.byline.ytmusic-player-bar') return {textContent:'Recording artist'};
  if (selector === 'img.image.ytmusic-player-bar') return {src:`https://i.ytimg.com/vi/${imageId}/hqdefault.jpg`};
  return null;
}};
let runtime;
const context = vm.createContext({window, document, navigator:{mediaSession:{metadata:null}}, URL,
  location:{href:'https://music.youtube.com/'}, Date:{now:()=>now}, Intl,
  chrome:{runtime:{onMessage:{addListener(fn) {runtime=fn;}},sendMessage(msg) {sent.push(msg);}}},
  setInterval(fn) {intervals.push(fn);return intervals.length;}, clearInterval() {},
  setTimeout() {}, clearTimeout() {}, console:{log() {}, warn() {}},
});
const flush = () => { while (queued.length) {const data=queued.shift();
  for (const fn of handlers) fn({source:window,data});} };
vm.runInContext(fs.readFileSync('extension/content.js','utf8'),context);
vm.runInContext(fs.readFileSync('extension/nowplaying_main.js','utf8'),context);
flush();
sent.length=0;
runtime({type:'resync-now'},{},()=>{});
assert(queued.some(msg => msg.__tcSensorProbe), 'reconnect must request a fresh MAIN sample');
flush();
const latest = sent.filter(msg => ['play','now-heartbeat'].includes(msg.type)).at(-1);
assert.equal(latest.videoId,'current', 'MAIN player identity must win over the old player bar');
assert.equal(latest.position,20, 'reconnect carries a fresh interpolation anchor');
assert.equal(latest.duration,null,'the app supplies the known track length');
if (['transition','conflicting_bar'].includes(scenario)) {
  assert.equal(latest.type,'now-heartbeat');
  assert.equal(latest.metadataVerified,false);
  assert.equal(sent.filter(msg => msg.type==='play').length,0);
  now += 6000;
  intervals[0](); // DOM-only fallback cannot promote the explicitly unverified sample.
  flush();
  assert.equal(sent.filter(msg => msg.type==='play').length,0);
  barId=imageId='current';
  intervals[1](); flush();
  const plays=sent.filter(msg => msg.type==='play');
  assert.equal(plays.length,1);
  assert.equal(plays[0].videoId,'current');
  assert.equal(plays[0].artist,'Recording artist');
  assert.equal(plays[0].metadataVerified,true);
} else {
  assert.equal(latest.metadataVerified,true);
  assert.equal(latest.paused,scenario==='paused');
  assert.equal(sent.filter(msg => msg.type==='play').length,scenario==='paused' ? 0 : 1);
}
if (scenario === 'continuous_stream') {
  // Actual Time Hurts heartbeats had these changing totals for the same 4:18 song.
  // The extension must carry elapsed time without forwarding the growing buffer as a total.
  for (const [elapsed, buffered] of [
    [11.554541437641547,49.886619437641684],
    [37.55412543764169,109.79410243764164],
    [61.55221643764162,169.7015854376416],
    [140,258],
  ]) {
    position=elapsed; bufferedDuration=buffered;
    intervals[1](); flush();
    assert.equal(sent.at(-1).position,elapsed);
    assert.equal(sent.at(-1).duration,null,'buffering is not a progress denominator');
  }
  // The same media stream keeps advancing across songs; only the player clock resets.
  playerId=barId=imageId='next';
  position=0;
  video.currentTime=665; video.duration=720;
  intervals[1](); flush();
  assert.equal(sent.at(-1).videoId,'next');
  assert.equal(sent.at(-1).position,0,'a new track starts at zero despite the continuous stream');
  assert.equal(sent.at(-1).duration,null);
  position=4; video.currentTime=669; video.duration=740;
  intervals[1](); flush();
  assert.equal(sent.at(-1).position,4,'progress must advance without a media reset');
  position=180;
  intervals[1](); flush();
  assert.equal(sent.at(-1).position,180,'a seek uses the native song clock');
  playerId=barId=imageId='third';
  position=17;
  intervals[1](); flush();
  assert.equal(sent.at(-1).position,17,'a new song can begin at a nonzero seek');
  assert.equal(sent.at(-1).duration,null);
}
// Subsequent heartbeats carry seeks, pause and unknown timing through the real sensor/receiver.
video.currentTime=0;
position=0;
video.paused=true;
intervals[1](); flush();
let heartbeat=sent.at(-1);
assert.equal(heartbeat.type,'now-heartbeat');
assert.equal(heartbeat.position,0);
assert.equal(heartbeat.duration,null);
assert.equal(heartbeat.paused,true);
video.currentTime=NaN; video.duration=Infinity;
position=NaN;
intervals[1](); flush();
heartbeat=sent.at(-1);
assert.equal(heartbeat.position,null);
assert.equal(heartbeat.duration,null);
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
