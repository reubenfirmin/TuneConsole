"""Opt-in audio experiment integrity; synthetic checks are never listening labels."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / 'experiments' / 'audio_lab'


def _node(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio lab checks')
    result = subprocess.run([node, '--input-type=module', '-e', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_bounded_windows_identity_discontinuities_and_native_measurements():
    _node(r"""
import assert from 'node:assert/strict';
import {Sampler, windows, coverage} from './experiments/audio_lab/sampling.js';
import {describe} from './experiments/audio_lab/features.js';
assert.deepEqual(windows(100), [{start:2,end:12},{start:50,end:60},{start:88,end:98}]);
assert.deepEqual(windows(5), [{start:.5,end:4}]);
assert.deepEqual(windows(Infinity), []);
assert.deepEqual(coverage([{start:2,end:12},{start:2,end:12},{start:8,end:15}],100), {seconds:13,fraction:.13});
assert.throws(() => new Sampler(384000, () => {}));
const source = 'sha256:' + 'a'.repeat(64), rate = 16000;
function state(t, extra={}) {
  return {source,epoch:0,position:t,duration:30,rate:1,paused:false,permitted:true,at:t*1000,...extra};
}
function feed(sampler, until, change=()=>({})) {
  for (let frame=0; frame<until*rate; frame+=1600) {
    const t=frame/rate, s=state(t, change(t));
    sampler.update(s, t, t*1000);
    const block=Float32Array.from({length:1600},(_,i)=>.25*Math.sin(2*Math.PI*440*(frame+i)/rate));
    sampler.push(block,t,`${s.source}/${s.epoch}`,t*1000);
    assert(!sampler.buffer || sampler.buffer.samples.length <= rate*10);
  }
}
const heard=[], sampler=new Sampler(rate, w=>heard.push(w));
feed(sampler, 28);
assert.equal(heard.length,2);
assert.equal(heard[0].start,2);
assert(Math.abs(heard[0].end-12)<.025);
const d=describe(heard[0].samples,rate);
assert(Math.abs(d.rms-.25/Math.sqrt(2))<.001);
assert(Math.abs(d.zero_crossings_per_second-880)<1);
assert(!('intensity' in d));
assert.equal(describe(new Float32Array(100),rate).rms_dbfs,null);
assert.throws(()=>describe(Float32Array.of(NaN),rate));
for (const change of [t=>({permitted:false}),t=>({rate:2}),t=>({paused:true}),
    t=>({epoch:t>=8?1:0,position:t>=8?t+8:t}),
    t=>({epoch:t>=8?1:0,paused:t>=8}), t=>({source:t>=8?'unknown':source})]) {
  const out=[], s=new Sampler(rate,w=>out.push(w));
  feed(s,14,change);
  assert.equal(out.length,0,'invalid or interrupted windows must abstain');
}
const stale=[]; const s=new Sampler(rate,w=>stale.push(w));
s.update(state(2),2,2000);
s.push(new Float32Array(1600),2,`${source}/0`,2800);
assert.equal(stale.length,0); assert.equal(s.buffer,null);
// Missing audio blocks cannot be silently replaced by later frames.
s.update(state(2),2,2000); s.push(new Float32Array(1600),2,`${source}/0`,2000);
s.update(state(3),3,3000); s.push(new Float32Array(1600),3,`${source}/0`,3000);
assert.equal(s.buffer,null); assert(s.discarded>0);
const partial=[]; const short=new Sampler(rate,w=>partial.push(w));
for(let frame=0;frame<rate*5;frame+=1600) {
  const t=frame/rate; short.update(state(t,{duration:5}),t,t*1000);
  short.push(new Float32Array(1600),t,`${source}/0`,t*1000);
}
assert.equal(partial.length,1); assert.equal(partial[0].partial,true);
""")


def test_capture_requires_toolbar_and_reference_consent_and_releases_resources():
    _node(r"""
import assert from 'node:assert/strict';
import {CaptureController} from './experiments/audio_lab/background.js';
const root='chrome-extension://fixture/', events=[];
let docs=[], denied=false, creates=0;
const api={
  runtime:{getURL:path=>root+path, getContexts:async query=>query.contextTypes[0]==='TAB'?[{tabId:7}]:docs,
    sendMessage:async message=>{events.push(message);return {ok:true};}},
  offscreen:{closeDocument:async()=>{docs=[];events.push('closed');},
    createDocument:async()=>{creates++;docs=[{}];}},
  tabCapture:{getMediaStreamId:async()=>{if(denied)throw Error('permission denied');return 'stream';}},
  action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}},
  tabs:{create:async t=>events.push(t)},
};
const c=new CaptureController(api); await c.ready;
const tab={id:7,url:root+'reference.html'}, sender={url:tab.url,documentId:'doc'};
await c.action(tab); assert.equal(creates,0);
const state={source:'sha256:'+'a'.repeat(64),permitted:true,permission_note:'Own recording',at:Date.now()};
await c.message({target:'background',type:'state',state},sender);
await c.message({target:'background',type:'start'},sender);
assert.equal(creates,0,'messages cannot start capture');
await c.action(tab); assert(c.active); assert.equal(creates,1);
await c.action(tab); assert.equal(c.active,null); assert.equal(docs.length,0);
denied=true; await c.action(tab); assert.equal(c.active,null); assert.equal(docs.length,0);
assert(events.some(e=>e.error==='permission denied'));
denied=false;
await c.action(tab); await c.message({target:'background',type:'state',state:{...state,source:'changed'}},sender);
assert.equal(c.active,null); assert.equal(docs.length,0);
await c.message({target:'background',type:'state',state},sender);
await c.action(tab); await c.updated(7,{status:'loading'}); assert.equal(docs.length,0);
await c.message({target:'background',type:'state',state},sender);
await c.action(tab); await c.removed(7); assert.equal(docs.length,0);
docs=[{}]; const restarted=new CaptureController(api); await restarted.ready;
assert.equal(docs.length,0); assert.equal(restarted.active,null);
await c.action({id:9,url:'https://music.youtube.com/'});
assert.equal(creates,5,'a non-reference source must only open the lab');
assert(events.some(e=>e.url===root+'reference.html'));
""")


def test_lab_has_no_remote_source_access_and_is_outside_store_package():
    assert not [path.relative_to(LAB) for path in LAB.rglob('*') if path.name.startswith('_')], 'Chrome reserves filenames starting with underscores'
    assert not list(LAB.rglob('*.py')), 'Python tooling must stay outside the loadable extension to avoid __pycache__'
    manifest = json.loads((LAB / 'manifest.json').read_text())
    assert set(manifest['permissions']) == {'activeTab', 'tabCapture', 'offscreen'}
    assert not manifest.get('host_permissions')
    assert not manifest.get('content_scripts')
    csp = manifest['content_security_policy']['extension_pages']
    assert "connect-src 'self'" in csp
    assert "script-src 'self' 'wasm-unsafe-eval'" in csp
    assert "'unsafe-eval'" not in csp
    production = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
    assert 'tabCapture' not in production['permissions']
    assert 'offscreen' not in production['permissions']


def test_worklet_mixes_channels_and_bounds_queued_pcm():
    _node(r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
let Processor;
const sent=[];
const realm={sampleRate:16000,currentTime:0,Float32Array,
  AudioWorkletProcessor:class{constructor(){this.port={postMessage:m=>sent.push(m)};}},
  registerProcessor:(name,p)=>{Processor=p;}};
vm.runInNewContext(fs.readFileSync('./experiments/audio_lab/pcm_worklet.js','utf8'),realm);
const p=new Processor(); p.port.onmessage({data:{token:'recording/1'}});
const channels=[new Float32Array(128).fill(.5),new Float32Array(128).fill(-.25)];
for(let i=0;i<200;i++){realm.currentTime=i*128/16000;p.process([channels]);}
assert.equal(sent.length,1,'a stalled consumer must not accumulate PCM messages');
assert(sent[0].samples.every(x=>x===.125),'stereo must be averaged, not summed');
p.port.onmessage({data:{ack:true}});
for(let i=0;i<20;i++)p.process([channels]);
assert.equal(sent.length,2);
p.port.onmessage({data:{token:null}});
for(let i=0;i<20;i++)p.process([channels]);
assert.equal(sent.length,2,'disabled sampling must not queue raw audio');
""")
