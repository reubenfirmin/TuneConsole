"""The extension worker owns capture and keeps raw data in the music tab."""
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts.audio_model.bundle import prepare
from tests.audio_model_fixtures import write_model_fixture


def test_audio_bundle_checks_hashes_before_replacing_installation(tmp_path):
    source, destination = tmp_path / 'model', tmp_path / 'extension'
    write_model_fixture(source)
    prepare(source, destination)
    before = {path.relative_to(destination): path.read_bytes()
              for path in destination.rglob('*') if path.is_file()}
    (source / 'vendor/essentia-csp.js').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Audio artifact changed'):
        prepare(source, destination)
    assert {path.relative_to(destination): path.read_bytes()
            for path in destination.rglob('*') if path.is_file()} == before


def test_internal_broker_checks_assets_and_discards_cancelled_or_unsafe_results():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for audio broker checks')
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const event=()=>({listeners:[], addListener(fn){this.listeners.push(fn)}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const files=['musicnn/model.json','musicnn/group1-shard1of1.bin'].map(path=>({path,sha256:'a'.repeat(64)}));
let mode='missing',fetches=0;
const pending=[],sources=[],injected=[];
const context=vm.createContext({URL,setTimeout,clearTimeout,
  fetch:async url=>{
    fetches++;
    assert.equal(url,'chrome-extension://fixture/audio_model/vendor/bundle.json');
    if (mode==='failed') throw Error('not packaged');
    return {ok:mode!=='missing',json:async()=>({schema:1,files:mode==='malformed'?[]:files})};
  },chrome:{runtime:{getURL:path=>'chrome-extension://fixture/'+path},
    scripting:{executeScript:async data=>injected.push(data)},tabs:{connect(_id,options){
      assert.equal(options.name,'tc-audio-preview-capture');
      const source={onMessage:event(),onDisconnect:event(),postMessage(message){this.request=message},
        disconnect(){this.closed=true;for (const fn of this.onDisconnect.listeners) fn()}};
      sources.push(source);
      return source;
    }}
  }});
vm.runInContext(fs.readFileSync('extension/worker/audio_capture.js','utf8'),context);
const create=vm.runInContext('createAudioCapture',context);
const makeBroker=()=>create({appOrigin:'http://127.0.0.1:8765',
  pickTab:()=>new Promise(resolve=>pending.push(resolve))});
const connection=broker=>{
  const messages=[];
  return {channel:broker.openCollection(message=>messages.push(message)),messages};
};
(async()=>{
  for (mode of ['missing','failed','malformed']) {
    const broker=makeBroker(),{channel,messages}=connection(broker);
    channel.postMessage({type:'capture',id:'too-early'});
    assert.equal(pending.length,0);
    await settle();
    assert.equal(messages[0].type,'unavailable');
    channel.postMessage({type:'capture',id:'unavailable'});
    assert.equal(pending.length,0);
    const previous=fetches;
    connection(broker);
    await settle();
    assert.equal(fetches,previous,'asset availability is cached for this worker');
  }
  mode='present';
  const broker=makeBroker(),first=connection(broker),second=connection(broker);
  await settle();
  assert.equal(first.messages[0].type,'ready');
  assert.equal(second.messages[0].type,'ready');
  assert(!broker.allowed({url:'https://music.youtube.com/',frameId:0}));
  assert(!broker.allowed({url:'http://127.0.0.1:8765/',frameId:1}));
  assert(broker.allowed({url:'http://127.0.0.1:8765/',frameId:0}));
  first.channel.postMessage({type:'capture',id:'old'});
  first.channel.postMessage({type:'stop'});
  first.channel.postMessage({type:'capture',id:'fresh',video_id:'song',not_before:30});
  pending.shift()({id:7});
  await settle();
  assert.equal(injected.length,0,'a cancelled request cannot install tab scripts');
  pending.shift()({id:7});
  await settle();
  assert.deepEqual(Array.from(injected[0].files),['audio_capture.js']);
  assert.equal(sources[0].request.not_before,30);
  second.channel.postMessage({type:'capture',id:'blocked'});
  assert.match(second.messages.at(-1).error,/Another audio capture/);
  await sources[0].onMessage.listeners[0]({id:'fresh',type:'analysing'});
  second.channel.postMessage({type:'capture',id:'still-blocked'});
  assert.match(second.messages.at(-1).error,/Another audio capture/);
  const embedding={version:'musicnn-embedding-1',layer:'model/dense/BiasAdd',
    graph_policy:'restore-fused-preactivation-1',pooling:'mean_full_patches',patches:3,
    values:Array.from({length:200},(_,i)=>i/100-1)};
  const prediction={scores:Object.fromEntries(Array.from({length:50},(_,i)=>['tag'+i,.1])),
    model:'msd-musicnn-1',model_version:'1',embedding,bundle:{files}};
  const result={analysis_location:'youtube_tab',audio_transferred_to_app:false,sample_count:160000,
    raw_audio_retained:false,prediction};
  async function deliver(source,data,owner=7) {
    const done=source.onMessage.listeners[0](data);
    pending.shift()({id:owner});
    await done;
    assert.equal(source.closed,true);
  }
  await deliver(sources[0],{id:'fresh',type:'result',result});
  assert.equal(first.messages.at(-1).result.prediction.embedding.values.length,200);
  assert(!('moods' in first.messages.at(-1).result.prediction));
  async function start(id) {
    second.channel.postMessage({type:'capture',id});
    pending.shift()({id:7});
    await settle();
    return sources.at(-1);
  }
  const pcm=await start('pcm');
  await pcm.onMessage.listeners[0]({id:'pcm',type:'pcm',pcm:'raw bytes'});
  assert.match(second.messages.at(-1).error,/Only analysis metadata/);
  assert.equal(pcm.closed,true);
  const wrongTab=await start('wrong-tab');
  await deliver(wrongTab,{id:'wrong-tab',type:'result',result},8);
  assert.match(second.messages.at(-1).error,/playing music tab changed/);
  for (const [mutated,error] of [
    [{...prediction,embedding:undefined},/Invalid frozen music embedding/],
    [{...prediction,embedding:{...embedding,values:[1,2,3]}},/Invalid frozen music embedding/],
    [{...prediction,embedding:{...embedding,values:Array(200).fill(NaN)}},/Invalid frozen music embedding/],
    [{...prediction,embedding:{...embedding,pcm:[1]}},/Invalid frozen music embedding/],
    [{...prediction,embeddings:[1,2,3]},/Audio features cannot leave/],
    [{...prediction,nested:{embedding}},/Unexpected embedding location/],
    [{...prediction,other:Array(200).fill(0)},/Audio features cannot leave/],
  ]) {
    const source=await start('unsafe');
    await deliver(source,{id:'unsafe',type:'result',result:{...result,prediction:mutated}});
    assert.match(second.messages.at(-1).error,error);
  }
  // A terminal reply can synchronously let the collector reserve another point.
  const replies=[];
  const retry=broker.openCollection(message=>{
    replies.push(message);
    if (message.id==='interrupted')
      retry.postMessage({type:'capture',id:'middle',video_id:'song',not_before:100});
  });
  await settle();
  retry.postMessage({type:'capture',id:'interrupted',video_id:'song',not_before:30});
  pending.shift()({id:7});
  await settle();
  const interrupted=sources.at(-1);
  await interrupted.onMessage.listeners[0]({id:'interrupted',error:'Playback changed.'});
  assert.equal(interrupted.closed,true);
  assert.deepEqual(replies.map(message=>message.id),[undefined,'interrupted']);
  pending.shift()({id:7});
  await settle();
  const middle=sources.at(-1);
  assert.equal(middle.request.not_before,100);
  await deliver(middle,{id:'middle',type:'result',result});
  assert.equal(replies.at(-1).type,'result');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
