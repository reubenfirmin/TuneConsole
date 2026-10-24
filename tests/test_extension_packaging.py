"""The store ZIP must execute the same worker graph without adding permissions."""
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("bootstrap", ["current", "legacy_bootstrap", "missing_receiver_option"])
def test_store_package_loads_worker_modules_and_saved_playlist_radio(tmp_path, bootstrap):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the packaged worker smoke check")
    shutil.copytree(ROOT / "extension", tmp_path / "extension")
    if bootstrap != "current":
        background = tmp_path / "extension" / "background.js"
        source = background.read_text().replace("receiverRevision: TC_CONFIG.receiverRevision, ", "")
        if bootstrap == "legacy_bootstrap":
            source = source.replace("revision: TC_CONFIG.sensorRevision, ", "")
        background.write_text(source)
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "build-extension.sh", tmp_path / "scripts" / "build-extension.sh")
    built = subprocess.run(["bash", str(tmp_path / "scripts" / "build-extension.sh")],
                           capture_output=True, text=True, timeout=20)
    assert built.returncode == 0, built.stderr
    source = json.loads((ROOT / "extension" / "manifest.json").read_text())
    archive, = (tmp_path / "dist").glob("*.zip")
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest == {key: value for key, value in source.items() if key != "key"}
        assert "README.md" not in package.namelist()
        package.extractall(tmp_path / "unpacked")
    script = r"""
const assert = require('node:assert/strict');
const calls = [], events = [];
const event = () => ({addListener(fn) { events.push(fn); }});
const chrome = {
  runtime: {onMessage:event(), onStartup:event(), onInstalled:event()},
  tabs: {query:async () => [{id:7,url:'https://music.youtube.com/'}],
         sendMessage:async () => ({ok:true,receiverRevision:9,mainVersion:4,mainRevision:9,mainAgeMs:0}),
         onRemoved:event(), onUpdated:event()},
  alarms: {create() {}, onAlarm:event()},
  scripting: {executeScript:async call => {
    calls.push(call);
    return [{result:{ok:true,playback_synced:true}}];
  }},
};
class Socket { static OPEN = 1; static CLOSED = 3; constructor() { this.readyState=0; } }
const {radio, sensors} = require('./tests/extension_worker.cjs').loadWorker({chrome, WebSocket:Socket,
  URL, console, setTimeout() {}, clearTimeout() {}}, process.argv[1]);
(async () => {
  const result = await radio.update({playlist:'PL_fixture',videoIds:['first','next'],refresh:1,id:42});
  assert.equal(result.playback_synced,true);
  assert(calls.some(call => call.files?.includes('music_setup.js')),
    'setup must reach an already-open tab without a backend connection or install event');
  const radioCalls = calls.filter(call => !call.files?.includes('music_setup.js'));
  assert.deepEqual([...radioCalls[0].files],['radio_session.js']);
  assert.equal(radioCalls[0].world,'MAIN');
  assert.equal(radioCalls[1].target.tabId,7);
  assert.deepEqual(JSON.parse(JSON.stringify(radioCalls[1].args)),
    [{playlist:'PL_fixture',videoIds:['first','next'],refresh:true}]);
  assert.equal((await sensors.probe()).healthy,true,
    'the packaged bootstrap must accept its own sensor revisions');
  assert.equal((await sensors.ping(7,true,{nativeRating:true})).ok,true);
})().catch(error => { console.error(error); process.exitCode=1; });
"""
    result = subprocess.run([node, "-e", script, str(tmp_path / "unpacked")], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
