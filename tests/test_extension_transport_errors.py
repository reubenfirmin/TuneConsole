"""Correlated bridge completion without replaying possibly completed mutations."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario", [
    "tab_setup", "cookie", "digest", "injection", "missing_receiver", "lost_reply", "success",
    "dispatch", "replacement", "malformed",
])
def test_request_completion_and_receiving_socket_ownership(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the worker harness")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const scenario = process.argv[1], sockets = [], warnings = [];
let sends = 0, injections = 0, dispatches = 0, release;
const secret = 'private-cookie https://music.youtube.com/?token=secret';
const fail = () => { throw Error(secret); };
class Socket {
  static OPEN = 1; static CLOSED = 3;
  constructor() { this.readyState = 1; this.frames = []; sockets.push(this); }
  send(raw) { this.frames.push(JSON.parse(raw)); }
}
const ctx = vm.createContext({WebSocket: Socket, URL, TextEncoder, Date,
  crypto: {subtle: {digest: async () => {
    if (scenario === 'digest') fail(); return new ArrayBuffer(20);
  }}},
  chrome: {
    cookies: {get: async () => { if (scenario === 'cookie') fail(); return {value: secret}; }},
    tabs: {sendMessage: async () => {
      sends++;
      if (['missing_receiver', 'injection'].includes(scenario) && sends === 1)
        throw Error('missing receiver');
      if (scenario === 'lost_reply') fail();
      return {status: 200, body: '{}', timeZone: 'America/Costa_Rica'};
    }},
  },
  isMissingReceiver: e => e.message === 'missing receiver',
  setTimeout() {},
});
for (const name of ['requests', 'transport'])
  vm.runInContext(fs.readFileSync(`extension/worker/${name}.js`, 'utf8'), ctx);
const requests = vm.runInContext('createRequestForwarder', ctx)({
  origin: 'https://music.youtube.com',
  tabs: {ensure: async () => { if (scenario === 'tab_setup') fail(); return {id: 1}; }},
  sensors: {inject: async () => { injections++; if (scenario === 'injection') fail(); return {ok: true}; }},
});
const transport = vm.runInContext('createBridgeTransport', ctx)({
  url: 'ws://bridge', onStatus() {}, onOpen() {}, warn: msg => warnings.push(msg),
  dispatch: async frame => {
    dispatches++;
    if (scenario === 'dispatch') fail();
    if (scenario === 'replacement') await new Promise(resolve => release = resolve);
    return requests.handle(frame);
  },
});
const request = {id: 42, method: 'POST', url: 'https://music.youtube.com/youtubei/v1/edit_playlist', body: {}};
(async () => {
  transport.connect();
  const first = sockets[0];
  if (scenario === 'malformed') {
    for (const data of ['{', 'null', '[]', '{}', '{"id":"secret"}', '{"id":0}'])
      await first.onmessage({data});
    assert.equal(warnings.length, 6);
    assert.equal(dispatches, 0);
    assert.equal(first.frames.length, 0);
    assert(warnings.every(message => message.length < 100 && !message.includes('secret')));
    return;
  }
  const pending = first.onmessage({data: JSON.stringify(request)});
  if (scenario === 'replacement') {
    await new Promise(resolve => setImmediate(resolve));
    first.readyState = Socket.CLOSED; transport.connect(); release();
  }
  await pending;
  assert.equal(first.frames.length, 1);
  const reply = first.frames[0], body = JSON.parse(reply.body);
  assert.equal(reply.id, 42);
  if (['success', 'replacement', 'missing_receiver'].includes(scenario)) {
    assert.equal(reply.status, 200);
    assert.equal(reply.timeZone, 'America/Costa_Rica');
  } else {
    assert.equal(reply.status, 0);
    assert.equal(body.outcome, ['lost_reply', 'dispatch'].includes(scenario) ? 'uncertain' : 'not-dispatched');
    assert(!reply.body.includes('private-cookie') && !reply.body.includes('token=secret'));
    assert(reply.body.length < 300);
  }
  assert.equal(sends, scenario === 'missing_receiver' ? 2
    : ['success','replacement','lost_reply','injection'].includes(scenario) ? 1 : 0);
  assert.equal(injections, ['missing_receiver','injection'].includes(scenario) ? 1 : 0);
  if (scenario === 'replacement') assert.equal(sockets[1].frames.length, 0);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
