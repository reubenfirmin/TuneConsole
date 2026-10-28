"""Unknown curation cannot pass a stale receiver; observer updates stay idempotent."""
from pathlib import Path
import shutil
import subprocess

import pytest


def run_node(script, *args):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for the extension curation harness')
    result = subprocess.run([node, '-e', script, *args], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_worker_rejects_unknown_and_legacy_curation_and_preserves_request_identity():
    run_node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context=vm.createContext({console,URL,setTimeout,clearTimeout});
for(const file of ['ratings','playback'])
  vm.runInContext(fs.readFileSync(`extension/worker/${file}.js`,'utf8'),context);
const frames=[], relay=vm.runInContext('createPlayback',context)({
  musicTabs:{list:async()=>[]},sensors:{},send:message=>frames.push(message),
  warn:message=>{throw Error(message);},
});
(async()=>{
  for(const kind of ['rate','playlist_edit','feedback','subscription','share_intent']) {
    for(const brandKnown of [undefined,false])
      relay.receive({type:'pevent',kind,brandId:'',brandKnown},{tab:{id:7}});
    for(const brandId of ['', 'request-account'])
      relay.receive({type:'pevent',kind,brandKnown:true,brandId},{tab:{id:7}});
  }
  await relay.drained;
  assert.equal(frames.length,10);
  for(let index=0;index<frames.length;index++) {
    assert.equal(frames[index].brandKnown,true);
    assert.equal(frames[index].brandId,index%2?'request-account':'');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


@pytest.mark.parametrize('previous', ['legacy', 'older'])
def test_observer_reinjection_updates_legacy_hook_without_duplicate_current_observations(previous):
    run_node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  const previous=process.argv[1], source=fs.readFileSync('extension/observer_main.js','utf8');
  const sent=[], calls=[];
  let settle;
  const promise=new Promise(resolve=>{settle=resolve;});
  const window={fetch:(...args)=>{calls.push(args);return promise;},
    postMessage:message=>sent.push(message.__tcCuration),
    ytcfg:{get:key=>key==='LOGGED_IN'?true:''}};
  const context=vm.createContext({window,location:{href:'https://music.youtube.com/'},Promise});
  const url='https://music.youtube.com/youtubei/v1/like/like';
  const init={method:'POST',body:JSON.stringify({target:{videoId:'v'}})};
  if(previous==='legacy') window.__tcFetchHook=true;
  else {
    vm.runInContext(source.replace('var REVISION = 2;', 'var REVISION = 1;'),context);
    assert.equal(window.fetch(url,init),promise);
  }
  const oldHook=window.__tcFetchHook;
  vm.runInContext(source,context);
  assert.equal(window.__tcFetchHook.revision,2);
  if(previous==='older') assert.equal(oldHook.stopped,true);
  const installed=window.fetch;
  vm.runInContext(source,context);
  assert.equal(window.fetch,installed,'same-revision reinjection must not wrap fetch again');
  assert.equal(window.fetch(url,init),promise);
  settle(new Response('',{status:200}));
  await promise; await new Promise(setImmediate);
  assert.equal(sent.length,1,'a replaced observer cannot report its pending or future requests');
  assert.equal(sent[0].brandKnown,true);
  assert.equal(sent[0].brandId,'');
  assert.equal(calls.length,previous==='older'?2:1,'fetch is never replayed by observation');
})().catch(error=>{console.error(error);process.exitCode=1;});
""", previous)
