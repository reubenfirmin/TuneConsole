"""Application audio preview: read-only assets and bounded capture lifecycle."""
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from yt_playlist.web.routes import audio_preview
from experiments.audio_lab_tools.prepare_extension import prepare


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


def test_optional_extension_preparer_checks_hashes_before_replacing_installation(tmp_path):
    import hashlib
    import json

    lab, destination = tmp_path / 'lab', tmp_path / 'extension'
    vendor = lab / 'vendor'
    vendor.mkdir(parents=True)
    for name in ('model_worker.js', 'runtime_loader.js', 'features.js', 'embedding_graph.js', 'audio_attributes.js'):
        (lab / name).write_text('// fixture')
    artifact = b'verified runtime'
    (vendor / 'essentia-csp.js').write_bytes(artifact)
    row = {'path': 'essentia-csp.js', 'bytes': len(artifact), 'sha256': hashlib.sha256(artifact).hexdigest()}
    (vendor / 'runtime.json').write_text(json.dumps({'runtime': 'essentia-csp-audio-2',
        'flags': ['-sDYNAMIC_EXECUTION=0'], 'files': [row]}))
    meyda = []
    for name in ('meyda.min.js', 'meyda-LICENSE'):
        (vendor / name).write_bytes(artifact)
        meyda.append({**row, 'path': name})
    (vendor / 'bundle.json').write_text(json.dumps({'meyda_version': '5.6.3',
        'mood_heads': [{'id': name} for name in ('relaxed', 'aggressive', 'happy', 'sad', 'deam')], 'files': meyda}))
    prepare(lab, destination)
    before = {str(path.relative_to(destination)): path.read_bytes() for path in destination.rglob('*') if path.is_file()}
    (vendor / 'essentia-csp.js').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Research artifact changed'):
        prepare(lab, destination)
    assert {str(path.relative_to(destination)): path.read_bytes() for path in destination.rglob('*') if path.is_file()} == before


def test_session_leaves_capture_validation_to_source_and_ignores_late_pcm():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio lifecycle checks')
    script = r"""
import assert from 'node:assert/strict';
import {AudioPreviewSession} from './src/yt_playlist/web/static/audio-preview-session.js';
let changes=[], messages=[], results=[], failures=[];
let playing={video_id:'track', title:'Track', paused:false, metadata_verified:true, position:20, at:Date.now()};
const session=new AudioPreviewSession({playback:()=>playing, changed:s=>changes.push(s),
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
const s={playing:{...playing}, started:Date.now()};
session.current=s;
playing={...playing,video_id:'another'};
assert.equal(session.check(s),true,'HTTP playback changes cannot invalidate the source recording');
playing={...playing,position:100,at:Date.now()-15000};
assert.equal(session.check(s),true,'stale status and corrected clocks must not cancel captures');
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
assert.equal(session.check(finishing),true);
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
"""
    result = subprocess.run([node, '--input-type=module', '-e', script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_unattended_collection_requires_extension_model_and_backs_off_when_unavailable():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio capability checks')
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {AudioPreviewSession} from './src/yt_playlist/web/static/audio-preview-session.js';
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
  vm.runInContext(fs.readFileSync('extension/worker/audio_preview.js','utf8'),context);
  const broker=vm.runInContext('createAudioPreview',context)({appOrigin:'http://127.0.0.1:8765',pickTab:async()=>{picks++;return null}});
  const p=port();broker.connect(p);
  await p.onMessage.listeners[0]({type:'capture',id:'too-early'});
  assert.equal(picks,0,'capture is blocked until packaged-model readiness');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(p.sent[0].type,mode==='present'?'ready':'unavailable');
  await p.onMessage.listeners[0]({type:'capture',id:'after-check'});
  assert.equal(picks,mode==='present'?1:0,'missing models must never trigger audio access');
  broker.connect(port());
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(fetches,1,'local asset availability is cached for this worker');
}
const timers=[],originalSetTimeout=globalThis.setTimeout,originalClearTimeout=globalThis.clearTimeout;
globalThis.setTimeout=(fn,delay)=>{timers.push({fn,delay});return timers.length};
globalThis.clearTimeout=()=>{};
const p=port(),connections=[];
globalThis.chrome={runtime:{connect:(id,options)=>{connections.push({id,options});return p}}};
const session=new AudioPreviewSession({playback:()=>({video_id:'playing',position:30,paused:false,at:Date.now()}),
  changed:()=>{},result:()=>{throw Error('unexpected result')},automatic:false,portName:'tc-audio-collection'});
session.extensionIds=['fixture'];session.connect();
assert.equal(connections[0].options.name,'tc-audio-collection');
for(const listener of p.onMessage.listeners) listener({type:'unavailable'});
assert.equal(session.port,null);
session.start();
assert.equal(p.sent.length,0,'an unavailable extension cannot start recording');
session.connect();
assert.equal(connections.length,1);
assert(timers.at(-1).delay>=59000,'unavailable models back off for a minute');
session.destroy();
globalThis.setTimeout=originalSetTimeout;globalThis.clearTimeout=originalClearTimeout;
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
vm.runInContext(fs.readFileSync('extension/worker/audio_preview.js','utf8'),context);
const create=vm.runInContext('createAudioPreview',context);
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
  await dispatch(second,{type:'capture',id:'blocked'});
  assert.match(second.sent.at(-1).error,/Another audio preview/);
  await sources[0].onMessage.listeners[0]({id:'fresh',type:'analysing'});
  await dispatch(second,{type:'capture',id:'still-blocked'});
  assert.match(second.sent.at(-1).error,/Another audio preview/,'lease includes model inference');
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
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
