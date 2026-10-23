"""The fetch observer confirms a rating once without changing the page's fetch contract."""
from pathlib import Path
import shutil
import subprocess

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
