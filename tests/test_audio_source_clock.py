"""Sampling targets follow the song clock even when native media spans songs."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_source_waits_for_song_clock_and_rejects_stale_or_changed_sources():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for source-clock checks')
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('extension/audio_preview_capture.js','utf8');
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)},removeListener(fn){
  this.listeners=this.listeners.filter(value=>value!==fn)}});
function fixture() {
  let now=0,captures=0,next=1,oldStopped=0;
  const timers=new Map(),listeners=new Map();
  const media={currentTime:1692,currentSrc:'stream',srcObject:null,paused:false,
    ended:false,readyState:4,isConnected:true,playbackRate:1,
    captureStream(){captures++;throw Error('Reached native capture')}};
  const elements=[media];
  const window={__tcAudioPreviewCapture:{revision:1,stop(){oldStopped++}},
    addEventListener(type,fn){listeners.set(type,fn)},removeEventListener(type){listeners.delete(type)}};
  const chrome={runtime:{id:'extension',onConnect:event()}};
  const context=vm.createContext({window,chrome,location:{origin:'https://music.youtube.com'},
    document:{querySelectorAll:()=>elements},Date:class extends Date{static now(){return now}},
    MediaRecorder:class {static isTypeSupported(){return true}},
    setInterval(fn){const id=next++;timers.set(id,fn);return id},clearInterval(id){timers.delete(id)}});
  vm.runInContext(source,context);
  assert.equal(oldStopped,1,'on-demand injection upgrades the old capture controller');
  vm.runInContext(source,context);
  assert.equal(chrome.runtime.onConnect.listeners.length,1,'same-revision reinjection is idempotent');
  const port={name:'tc-audio-preview-capture',sender:{id:'extension'},onMessage:event(),
    onDisconnect:event(),sent:[],postMessage(data){this.sent.push(data)}};
  chrome.runtime.onConnect.listeners[0](port);
  return {media,elements,port,get captures(){return captures},
    status(position,videoId='song') {listeners.get('message')({source:window,data:{
      __tcSensorVersion:4,__tcSensorRevision:11,__tcNow:{videoId,position,paused:false}}})},
    start(){port.onMessage.listeners[0]({type:'capture',id:'check',video_id:'song',not_before:30},port)},
    tick(ms,delta=ms/1000){now+=ms;media.currentTime+=delta;
      for(const fn of [...timers.values()])fn()},
    running(){return window.__tcAudioPreviewCapture.inspect().running},
    close(){window.__tcAudioPreviewCapture.stop()}};
}
const offset=fixture();offset.media.currentTime+=29;offset.status(29);offset.start();
assert.equal(offset.captures,0,'raw media at 1721 seconds must not satisfy a 30-second song target');
offset.tick(750);assert.equal(offset.captures,0);
offset.tick(250);assert.equal(offset.captures,1,JSON.stringify(offset.port.sent));offset.close();
for(const missing of [undefined,null,NaN]) {
  const f=fixture();f.status(missing);f.start();f.tick(31000);
  assert.equal(f.captures,0,'missing song clocks cannot fall back to native currentTime');
  f.status(31);f.tick(0);assert.equal(f.captures,1);f.close();
}
const stale=fixture();stale.status(0);stale.start();stale.tick(31000);
assert.equal(stale.captures,0,'stale player clocks cannot authorize capture');
stale.status(31);stale.tick(0);assert.equal(stale.captures,1);stale.close();
const jump=fixture();jump.status(29);jump.start();jump.tick(10,500);
assert.equal(jump.captures,0,'native timeline jumps require a fresh song-clock anchor');
jump.status(29);jump.tick(1000);assert.equal(jump.captures,1);jump.close();
for(const change of [f=>f.status(0,'other'),f=>{f.media.paused=true},
    f=>{f.media.currentSrc='other'},f=>{f.media.srcObject={}},f=>{f.media.isConnected=false}]) {
  const f=fixture();f.status(0);f.start();change(f);f.tick(100);
  assert.equal(f.captures,0);assert.equal(f.running(),false);
  assert.match(f.port.sent.at(-1).error,/Playback changed/);f.close();
}
const disconnected=fixture();disconnected.status(0);disconnected.start();
disconnected.port.onDisconnect.listeners[0]();disconnected.tick(31000);
assert.equal(disconnected.captures,0);assert.equal(disconnected.running(),false);disconnected.close();
// A short buffer/source replacement must not consume a whole sampler retry.
const buffering=fixture();buffering.media.readyState=1;buffering.status(31);buffering.start();
assert.equal(buffering.port.sent.at(-1).type,'waiting');
assert.equal(buffering.running(),true);assert.equal(buffering.captures,0);
buffering.tick(500);buffering.media.readyState=4;buffering.status(31);buffering.tick(0);
assert.equal(buffering.captures,1,'capture resumes as soon as the same song has ready media');
buffering.close();
for(const change of [f=>f.elements.splice(0),f=>{f.media.paused=true},
    f=>{f.media.ended=true},f=>{f.media.readyState=1}]) {
  const f=fixture();change(f);f.status(31);f.start();f.tick(3000);
  assert.equal(f.captures,0);assert.equal(f.running(),false);
  const failure=f.port.sent.at(-1);
  assert.match(failure.error,/music_media_unavailable/);
  assert.match(failure.error,/elements=\d/);
  assert.match(failure.error,/paused=|elements=0/);
  assert(failure.error.length<=500,'only bounded media state leaves the music tab');f.close();
}
const replaced=fixture();replaced.elements.splice(0);replaced.status(31);replaced.start();
replaced.elements.push(replaced.media);replaced.status(31);replaced.tick(250);
assert.equal(replaced.captures,1);replaced.close();
for(const cancel of [f=>f.port.onDisconnect.listeners[0](),f=>f.status(0,'other')]) {
  const f=fixture();f.media.readyState=1;f.status(31);f.start();cancel(f);f.tick(3000);
  assert.equal(f.captures,0);assert.equal(f.running(),false);f.close();
}
"""
    result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
