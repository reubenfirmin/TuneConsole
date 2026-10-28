"""The fetch observer confirms a rating once without changing the page's fetch contract."""
from pathlib import Path
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("scenario", ["like", "dislike", "403", "500", "reject", "request", "override"])
def test_rating_fetch_outcome(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the extension observer harness")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  const scenario=process.argv[1], sent=[], calls=[];
  let resolve,reject;
  const original=new Promise((a,b)=>{resolve=a;reject=b;});
  const window={fetch:function(...args){calls.push([this,args]);return original;},
    postMessage:msg=>sent.push(msg.__tcCuration),ytcfg:{get:()=> 'brand'}};
  vm.runInNewContext(fs.readFileSync('extension/observer_main.js','utf8'),
    {window,location:{href:'https://music.youtube.com/'},Promise});
  const body=JSON.stringify({target:{videoId:'v'}});
  const action=scenario==='dislike'?'dislike':'like';
  const url=`https://music.youtube.com/youtubei/v1/like/${action}?key=x`;
  const request=['request','override'].includes(scenario) ? new Request(url,{method:'POST',body}) : url;
  const init=scenario==='request'?undefined:{method:'POST',body:scenario==='override'?'override':body};
  const returned=window.fetch(request,init);
  assert.equal(returned,original);
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],window);
  assert.equal(calls[0][1][0],request);
  assert.equal(calls[0][1][1],init);
  await new Promise(setImmediate);
  assert.equal(sent.length,0,'no confirmed event at request time');
  const response=new Response('untouched',{status:['403','500'].includes(scenario)?Number(scenario):200});
  if(scenario==='reject') {
    const error=new TypeError('offline');reject(error);
    await assert.rejects(returned,e=>e===error);
  } else {
    resolve(response);assert.equal(await returned,response);
    assert.equal(await response.text(),'untouched');
  }
  // Request.clone() body reads can finish after the response; allow the stream to drain.
  for(let i=0;i<20&&!sent.length;i++) await new Promise(r=>setTimeout(r,5));
  assert.equal(sent.length,1);
  const event=sent[0];
  assert.equal(event.brandKnown,true);
  assert.equal(event.brandId,'brand');
  assert.equal(event.phase,'settled');
  assert.equal(event.success,!['403','500','reject'].includes(scenario));
  assert.equal(event.httpStatus,scenario==='reject'?0:response.status);
  assert.equal(event.body,scenario==='override'?'override':body);
  if(request instanceof Request) assert.equal(await request.text(),body);
  await new Promise(setImmediate);assert.equal(sent.length,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('before,after', [
    ('missing', 'missing'), ('throws', 'throws'), ('empty', 'empty'), ('signed-out', 'signed-out'),
    ('main', 'main'), ('brand', 'brand'), ('main', 'brand'), ('brand', 'main'),
    ('missing', 'main'), ('throws', 'brand'),
])
def test_request_time_account_survives_fetch_wait_and_controls_ingestion(store, before, after):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for the extension observer harness')
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  const [before,after]=process.argv.slice(1), sent=[];
  let settle;
  const original=new Promise(resolve=>{settle=resolve;});
  const window={fetch:()=>original,postMessage:message=>sent.push(message.__tcCuration)};
  function configure(state) {
    delete window.ytcfg;
    if(state==='missing') return;
    window.ytcfg={get:key=>{
      if(state==='throws') throw Error('Config not available');
      if(key==='DELEGATED_SESSION_ID') return state==='brand'?'brand-A':'';
      if(key==='LOGGED_IN') return state==='main';
    }};
  }
  configure(before);
  vm.runInNewContext(fs.readFileSync('extension/observer_main.js','utf8'),
    {window,location:{href:'https://music.youtube.com/'},Promise});
  const body=JSON.stringify({target:{videoId:'known-video'}});
  const result=window.fetch('https://music.youtube.com/youtubei/v1/like/dislike', {method:'POST',body});
  assert.equal(result,original);
  configure(after);
  settle(new Response('',{status:200}));
  await result; await new Promise(setImmediate);
  assert.equal(sent.length,1);
  assert.equal(sent[0].brandKnown,['main','brand'].includes(before));
  assert.equal(sent[0].brandId,before==='brand'?'brand-A':'');
  process.stdout.write(JSON.stringify(sent[0]));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, '-e', script, before, after],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    main = store.identities.upsert_identity('Main', 'cred', None, True)
    brand = store.identities.upsert_identity('Brand', 'cred', 'brand-A', False)
    store.tracks.upsert_track('known-video', 'Song', 'Artist', '', None)
    from yt_playlist.library import player_events
    accepted = before in ('main', 'brand')
    assert player_events.handle_player_event(SimpleNamespace(store=store), event, 100) is accepted
    rows = store.player_events.player_events_since(0)
    assert len(rows) == int(accepted)
    expected = main if before == 'main' else brand if before == 'brand' else None
    if accepted:
        assert rows[0]['identity_id'] == expected
    for identity in (main, brand):
        rating = store.tracks.item_rating(identity, 'known-video')
        assert (rating is not None) is (identity == expected)
