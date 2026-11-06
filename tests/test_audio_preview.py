"""Application audio preview: read-only assets and bounded capture lifecycle."""
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from yt_playlist.web.routes import audio_preview
from scripts.audio_model.bundle import prepare
from tests.audio_model_fixtures import write_model_fixture


def test_preview_receives_metadata_and_exposes_no_audio_or_model_endpoints(store, tmp_path, monkeypatch):
    for name in audio_preview.ASSETS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
    monkeypatch.setattr(audio_preview, 'MODEL', tmp_path)
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        page = client.get('/audio-preview')
        assert page.status_code == 200
        assert 'Analyse again' in page.text
        assert 'automatically analyses' in page.text and 'no sharing dialog' in page.text
        assert 'edhplcadobipneepllhkkajckoammpnk' in page.text
        assert 'href="/#radio"' in page.text and 'href="/#directions"' in page.text
        assert 'type="file"' not in page.text and 'Use generated test tone' not in page.text
        assert 'href="/audio-preview"' in client.get('/').text
        assert 'Mood predictions' in page.text
        assert 'analysis results reach this page' in page.text
        for path in ['model_worker.js', 'vendor/tf.min.js', 'reference.html', 'background.js', 'README.md', 'fetch_model.py', '../manifest.json']:
            assert client.get('/audio-preview/assets/' + path).status_code == 404
        assert client.post('/audio-preview', json={'audio': 'cannot upload'}).status_code == 405
        (tmp_path / 'vendor/runtime.json').unlink()
        assert 'data-model-available="false"' in client.get('/audio-preview').text
        assert 'href="/audio-preview"' not in client.get('/').text


def test_audio_bundle_checks_hashes_before_replacing_installation(tmp_path):
    source, destination = tmp_path / 'model', tmp_path / 'extension'
    write_model_fixture(source)
    prepare(source, destination)
    before = {str(path.relative_to(destination)): path.read_bytes() for path in destination.rglob('*') if path.is_file()}
    (source / 'vendor/essentia-csp.js').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Audio artifact changed'):
        prepare(source, destination)
    assert {str(path.relative_to(destination)): path.read_bytes() for path in destination.rglob('*') if path.is_file()} == before


def test_session_leaves_capture_validation_to_source_and_ignores_late_pcm():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio lifecycle checks')
    script = r"""
import assert from 'node:assert/strict';
import {AudioCaptureSession} from './src/yt_playlist/web/static/audio-capture-session.js';
let changes=[], messages=[], results=[], failures=[];
let playing={video_id:'track', title:'Track', paused:false, metadata_verified:true, position:20, at:Date.now()};
const session=new AudioCaptureSession({playback:()=>playing, changed:s=>changes.push(s),
  result:value=>results.push(value),failure:value=>failures.push(value)});
session.port={postMessage:message=>messages.push(message),disconnect(){}};
session.tryAutomatic();
assert.equal(messages[0].type,'capture');
const id=messages[0].id;
session.stop();
assert.equal(messages.at(-1).type,'stop');
session.receive({id,type:'pcm',pcm:'late'});
assert.equal(session.current,null);
assert.equal(results.length,0);
session.tryAutomatic();
assert.equal(session.current,null,'Stop must not restart the same song');
session.start();
const s=session.current;
playing={...playing,video_id:'another'};
session.tryAutomatic();
assert.equal(session.current,s,'HTTP playback changes cannot invalidate the source recording');
playing={...playing,position:100,at:Date.now()-15000};
session.tryAutomatic();
assert.equal(session.current,s,'stale status and corrected clocks must not cancel captures');
session.receive({id,type:'result',result:{}});
assert.equal(session.current,s,'an earlier token cannot settle the current capture');
session.sourceClock={video_id:playing.video_id,position:1800,at:Date.now(),paused:false};
assert.equal(session.validPlaying(),null,'legacy raw media time cannot refresh a stale song clock');
session.sourceClock={...session.sourceClock,position:100,clock_basis:'ytm_song_clock'};
assert(session.validPlaying().position<101,'only the source song clock can refresh scheduling');
session.sourceClock=null;
session.receive({id:s.token,error:'The song changed before the excerpt finished.'});
assert.equal(session.current,null,'the source still cancels a real song change');
assert.equal(failures.at(-1).phase,'capturing');
for(const extra of [{paused:true},{muted:true},{at:Date.now()-7000},{position:NaN}]) {
  playing={video_id:'track',paused:false,metadata_verified:true,position:20,at:Date.now(),...extra};
  assert.equal(session.validPlaying(),null);
}
playing={video_id:'unverified',paused:false,metadata_verified:false,position:20,at:Date.now()};
session.tryAutomatic();
assert.equal(session.current.playing.metadata_verified,false);
session.receive({id:session.current.token,type:'result',result:{analysis_location:'youtube_tab',
  audio_transferred_to_app:false,prediction:{moods:{traits:{relaxed:{score:.8}}}}}});
assert.equal(results.length,1);
assert.equal(results[0].observed_playback.metadata_verified,false);
assert.equal(results[0].source_identity_verified,false);
assert.equal(results[0].recommendation_adoption,false);
playing={video_id:'another',paused:false,metadata_verified:true,position:20,at:Date.now()};
session.tryAutomatic();
assert.equal(session.current.playing.video_id,'another');
session.receive({id:session.current.token,type:'pcm',rate:16000,pcm:'malformed'});
assert.equal(session.current,null);
playing={video_id:'finishing',paused:false,position:30,at:Date.now()};
session.start();
const finishing=session.current;
session.receive({id:finishing.token,type:'analysing'});
// The audio is already captured: a pause and a stale app status must not
// discard an otherwise valid analysis result for that same excerpt.
playing={...playing,paused:true,at:Date.now()-9000};
session.receive({id:finishing.token,type:'result',result:{analysis_location:'youtube_tab',
  audio_transferred_to_app:false,prediction:{moods:{traits:{relaxed:{score:.8}}}}}});
assert.equal(results.length,2);
assert.equal(results.at(-1).observed_playback.video_id,'finishing');
playing={video_id:'failed',paused:false,position:30,at:Date.now()};
session.start();
session.receive({id:session.current.token,error:'The excerpt could not be decoded.'});
assert.equal(failures.at(-1).message,'The excerpt could not be decoded.');
assert.equal(session.current,null);
session.destroy();

// Chrome's local disconnect only notifies the peer. Handshake cleanup and
// retries must not depend on receiving our own onDisconnect event.
const original={setTimeout,clearTimeout,now:Date.now,chrome:globalThis.chrome};
let now=100000,sequence=0;
const timers=new Map(),ports=[];
globalThis.setTimeout=(fn,delay)=>{
  const id=++sequence;timers.set(id,{fn,due:now+delay});return id;
};
globalThis.clearTimeout=id=>timers.delete(id);
Date.now=()=>now;
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
globalThis.chrome={runtime:{connect(id,options){
  assert.equal(options.name,'tc-audio-preview');
  const port={sent:[],onMessage:event(),onDisconnect:event(),
    postMessage(data){this.sent.push(data)},disconnect(){this.closed=true},
    receive(data){this.onMessage.listeners.forEach(fn=>fn(data))},
    remoteDisconnect(){this.closed=true;this.onDisconnect.listeners.forEach(fn=>fn())}};
  ports.push(port);return port;
}}};
function elapse(ms){
  now+=ms;
  for(const [id,timer] of [...timers]){
    if(timer.due<=now && timers.delete(id)) timer.fn();
  }
}
let retrying;
try{
  retrying=new AudioCaptureSession({extensionIds:['first','second'],
    playback:()=>({video_id:'playing',paused:false,position:20,at:now}),
    changed(){},result(){assert.fail('No analysis result was delivered')}});
  await Promise.resolve();
  assert.equal(ports.length,2);
  ports[0].receive({type:'unavailable'});
  assert.equal(ports[0].closed,true);
  elapse(3000);
  assert.equal(ports[1].closed,true,'an unanswered candidate times out locally');
  assert.equal(retrying.connecting.size,0);
  assert(ports.every(port=>port.sent.length===0),'no capture before model readiness');
  elapse(500);
  elapse(56499);
  assert.equal(ports.length,2,'unavailable models retain the one-minute backoff');
  elapse(1);
  assert.equal(ports.length,4,'an unavailable model must not strand future retries');
  ports[0].receive({type:'ready'});
  assert.equal(retrying.port,null,'late readiness from a closed candidate is ignored');
  ports[3].receive({type:'ready'});
  assert.equal(retrying.port,ports[3]);
  assert.equal(ports[2].closed,true,'the other candidate is closed when a model is ready');
  assert.equal(retrying.connecting.size,0);
  assert.equal(ports[3].sent[0].type,'capture','readiness permits automatic preview');
  ports[2].receive({type:'ready'});
  assert.equal(ports[2].sent.length,0);
  assert.equal(retrying.port,ports[3]);
  ports[3].remoteDisconnect();
  assert.equal(retrying.current,null);
  elapse(2000);
  assert.equal(ports.length,6,'remote disconnection still permits retry');
  ports[4].remoteDisconnect();
  elapse(2000);
  assert.equal(ports.length,6,'the peer still owns its unanswered handshake');
  elapse(1000);
  assert.equal(ports[5].closed,true);
  assert.equal(retrying.connecting.size,0);
  assert(ports.slice(4).every(port=>port.sent.length===0),'mixed failures cannot capture before readiness');
  elapse(999);
  assert.equal(ports.length,6,'retry remains bounded after the last handshake expires');
  elapse(1);
  assert.equal(ports.length,8,'an early remote failure plus peer timeout cannot strand retry');
  elapse(3000);
  assert(ports.slice(6,8).every(port=>port.closed));
  elapse(500);
  assert.equal(ports.length,10,'handshake timeouts do not strand later attempts');
  ports[8].remoteDisconnect();
  ports[9].remoteDisconnect();
  assert.equal(timers.size,1,'failed candidates share one reconnect timer');
  elapse(2000);
  assert.equal(ports.length,12);
  ports[10].remoteDisconnect();
  elapse(2000);
  assert.equal(ports.length,12);
  assert.equal(timers.size,2,'the pending peer retains its deadline and one retry');
  retrying.destroy();
  assert(ports.slice(10).every(port=>port.closed));
  ports[11].receive({type:'ready'});
  elapse(60000);
  assert.equal(ports.length,12,'disposal prevents reconnect and late readiness');
  assert.equal(retrying.port,null);
  assert.equal(timers.size,0,'disposal clears every pending handshake timer');
}finally{
  retrying?.destroy();
  globalThis.setTimeout=original.setTimeout;globalThis.clearTimeout=original.clearTimeout;
  Date.now=original.now;globalThis.chrome=original.chrome;
}
"""
    result = subprocess.run([node, '--input-type=module', '-e', script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_collection_broker_requires_packaged_model_before_capture():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio capability checks')
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
function port() {
  return {name:'tc-audio-collection',sender:{url:'http://127.0.0.1:8765/',frameId:0},
    onDisconnect:event(),onMessage:event(),sent:[],postMessage(data){this.sent.push(data)},
    disconnect(){for(const listener of this.onDisconnect.listeners) listener()}};
}
for(const mode of ['missing','failed','malformed','present']) {
  let fetches=0,picks=0;
  const context=vm.createContext({URL,setTimeout,clearTimeout,chrome:{runtime:{getURL:path=>'chrome-extension://fixture/'+path}},
    fetch:async url=>{
      fetches++;assert.equal(url,'chrome-extension://fixture/audio_model/vendor/bundle.json');
      if(mode==='failed') throw Error('not packaged');
      return {ok:mode!=='missing',json:async()=>({schema:1,files:mode==='malformed'?[]:
        ['musicnn/model.json','musicnn/group1-shard1of1.bin'].map(path=>({path,sha256:'a'.repeat(64)}))})};
    }});
  vm.runInContext(fs.readFileSync('extension/worker/audio_capture.js','utf8'),context);
  const broker=vm.runInContext('createAudioCapture',context)({appOrigin:'http://127.0.0.1:8765',pickTab:async()=>{picks++;return null}});
  const p=port(),internal=broker.openCollection(data=>p.sent.push(data));
  internal.postMessage({type:'capture',id:'too-early'});
  assert.equal(picks,0,'capture is blocked until packaged-model readiness');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(p.sent[0].type,mode==='present'?'ready':'unavailable');
  internal.postMessage({type:'capture',id:'after-check'});
  assert.equal(picks,mode==='present'?1:0,'missing models must never trigger audio access');
  broker.openCollection(()=>{});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(fetches,1,'local asset availability is cached for this worker');
}
"""
    result = subprocess.run([node, '--input-type=module', '-e', script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_preview_broker_checks_sender_serializes_jobs_and_discards_cancelled_requests():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio broker checks')
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
function port(url='http://127.0.0.1:8765/audio-preview', frameId=0) {
  return {name:'tc-audio-preview',sender:{url,frameId},onDisconnect:event(),onMessage:event(),
    sent:[],postMessage(data){this.sent.push(data)},disconnect(){this.closed=true}};
}
const pending=[],sources=[],injected=[];
const context=vm.createContext({URL,setTimeout,clearTimeout,chrome:{
  scripting:{executeScript:async data=>injected.push(data)},
  tabs:{connect:()=>{const p=port();sources.push(p);return p}},
}});
vm.runInContext(fs.readFileSync('extension/worker/audio_capture.js','utf8'),context);
const create=vm.runInContext('createAudioCapture',context);
const broker=create({appOrigin:'http://127.0.0.1:8765',pickTab:()=>new Promise(resolve=>pending.push(resolve))});
const dispatch=(p,data)=>p.onMessage.listeners[0](data);
(async()=>{
  for(const [url,frame] of [
    ['https://music.youtube.com/audio-preview',0],['https://tuneconsole.com/audio-preview',0],
    ['http://127.0.0.1:8766/audio-preview',0],['http://example.test:8765/',0],
    ['http://127.0.0.1:8765/audio-preview',1]]) {
    const rejected=port(url,frame);broker.connect(rejected);assert.equal(rejected.closed,true);assert.equal(rejected.sent.length,0);
  }
  const first=port(), second=port('http://localhost:8765/');
  broker.connect(first);broker.connect(second);
  assert.equal(first.sent[0].type,'ready');assert.equal(second.sent[0].type,'ready');
  const old=dispatch(first,{type:'capture',id:'old'});
  await dispatch(first,{type:'stop'});
  const fresh=dispatch(first,{type:'capture',id:'fresh'});
  pending.shift()({id:7});await old;
  assert.equal(injected.length,0,'a cancelled asynchronous request cannot install or capture');
  pending.shift()({id:7});await fresh;
  assert.equal(sources.length,1);
  assert.deepEqual(Array.from(injected[0].files),['audio_capture.js']);
  await dispatch(second,{type:'capture',id:'blocked'});
  assert.match(second.sent.at(-1).error,/Another audio capture/);
  await sources[0].onMessage.listeners[0]({id:'fresh',type:'analysing'});
  await dispatch(second,{type:'capture',id:'still-blocked'});
  assert.match(second.sent.at(-1).error,/Another audio capture/,'lease includes model inference');
  const result={analysis_location:'youtube_tab',audio_transferred_to_app:false,sample_count:160000,
    raw_audio_retained:false,prediction:{scores:Object.fromEntries(Array.from({length:50},(_,i)=>['tag'+i,.1]))}};
  const complete=sources[0].onMessage.listeners[0]({id:'fresh',type:'result',result});
  pending.shift()({id:7});await complete;
  assert.equal(first.sent.at(-1).type,'result');assert.equal(sources[0].closed,true);
  assert(!JSON.stringify(first.sent).includes('"pcm"'));
  const next=dispatch(second,{type:'capture',id:'next'});
  pending.shift()({id:7});await next;
  await sources[1].onMessage.listeners[0]({id:'next',type:'pcm',rate:16000,pcm:'invalid'});
  assert.match(second.sent.at(-1).error,/Only analysis metadata/);assert.equal(sources[1].closed,true);
  const unsafe=dispatch(second,{type:'capture',id:'unsafe'});
  pending.shift()({id:7});await unsafe;
  const rejected=sources[2].onMessage.listeners[0]({id:'unsafe',type:'result',result:{...result,
    prediction:{...result.prediction,embeddings:[1,2,3]}}});
  pending.shift()({id:7});await rejected;
  assert.match(second.sent.at(-1).error,/Audio features cannot leave/);
  assert.equal(sources[2].closed,true);
  const embedding={version:'musicnn-embedding-1',layer:'model/dense/BiasAdd',
    graph_policy:'restore-fused-preactivation-1',pooling:'mean_full_patches',patches:3,
    values:Array.from({length:200},(_,i)=>i/100-1)};
  const prediction={...result.prediction,model:'msd-musicnn-1',model_version:'1',embedding,
    bundle:{files:['musicnn/model.json','musicnn/group1-shard1of1.bin'].map(path=>({path,sha256:'a'.repeat(64)}))}};
  async function finish(id,value) {
    const started=dispatch(second,{type:'capture',id});pending.shift()({id:7});await started;
    const source=sources.at(-1);
    const done=source.onMessage.listeners[0]({id,type:'result',result:value});
    pending.shift()({id:7});await done;
    return second.sent.at(-1);
  }
  const accepted=await finish('embedding',{...result,prediction});
  assert.equal(accepted.type,'result');
  assert.equal(accepted.result.prediction.embedding.values.length,200);
  assert.equal(accepted.result.prediction.embedding.values[0],-1);
  for(const malformed of [
    {...embedding,values:[1,2,3]}, {...embedding,values:Array(200).fill(NaN)},
    {...embedding,pooling:'unknown'}, {...embedding,pcm:[1]},
    {...embedding,values:Array(200).fill(true)}, {...embedding,patches:0}]) {
    assert.match((await finish('malformed',{...result,prediction:{...prediction,embedding:malformed}})).error,
      /Invalid frozen music embedding/);
  }
  assert.match((await finish('untyped',{...result,prediction:{...prediction,other:Array(200).fill(0)}})).error,
    /Audio features cannot leave/);
  assert.match((await finish('nested',{...result,prediction:{...prediction,moods:{embedding}}})).error,
    /Unexpected embedding location/);
  const absent=dispatch(second,{type:'capture',id:'absent'});
  pending.shift()(null);await absent;
  assert.match(second.sent.at(-1).error,/music_tab_unavailable/);
  const media=dispatch(second,{type:'capture',id:'media'});
  pending.shift()({id:7,audible:true,status:'complete'});await media;
  await sources.at(-1).onMessage.listeners[0]({id:'media',error:
    'Audio capture found no ready playing media [music_media_unavailable; elements=1; paused=0,ready=1]'});
  assert.match(second.sent.at(-1).error,/music_media_unavailable/);
  assert.match(second.sent.at(-1).error,/tab=7,audible=1/);
  assert.equal(sources.at(-1).closed,true);
  // Collection reservations have no app-document owner. Preview preempts a
  // waiting recording, but lets a completed excerpt finish durable delivery.
  const collectionMessages=[];
  const priority=create({appOrigin:'http://127.0.0.1:8765',pickTab:async()=>({id:7})});
  context.fetch=async()=>({ok:true,json:async()=>({schema:1,files:
    ['musicnn/model.json','musicnn/group1-shard1of1.bin'].map(path=>({path,sha256:'a'.repeat(64)}))})});
  context.chrome.runtime={getURL:path=>path};
  const collection=priority.openCollection(data=>collectionMessages.push(data));
  await new Promise(resolve=>setImmediate(resolve));
  collection.postMessage({type:'capture',id:'reserved',video_id:'song',not_before:120});
  await new Promise(resolve=>setImmediate(resolve));
  const reservation=sources.at(-1),preview=port();
  priority.connect(preview);
  assert.equal(reservation.closed,true,'opening preview cancels future reservations');
  assert.match(collectionMessages.at(-1).error,/preview is open/);
  await dispatch(preview,{type:'capture',id:'preview-priority'});
  assert.equal(sources.at(-1).closed,undefined);
  await dispatch(preview,{type:'stop'});
  for(const fn of preview.onDisconnect.listeners) fn();
  assert.equal(priority.previewOpen,false);
  collection.postMessage({type:'capture',id:'completed',video_id:'song'});
  await new Promise(resolve=>setImmediate(resolve));
  const completedSource=sources.at(-1);
  await completedSource.onMessage.listeners[0]({id:'completed',type:'analysing'});
  const waitingPreview=port();priority.connect(waitingPreview);
  assert.equal(completedSource.closed,undefined,'completed metadata is preserved');
  await dispatch(waitingPreview,{type:'capture',id:'wait'});
  assert.equal(waitingPreview.sent.at(-1).collection_finishing,true);
  await completedSource.onMessage.listeners[0]({id:'completed',type:'result',result});
  assert.equal(collectionMessages.at(-1).type,'result');
  await dispatch(waitingPreview,{type:'capture',id:'after-analysis'});
  assert.equal(sources.at(-1).closed,undefined);
  await dispatch(waitingPreview,{type:'stop'});
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
