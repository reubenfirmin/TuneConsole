"""The store ZIP must execute the same worker graph without adding permissions."""
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from scripts.audio_model.bundle import SCRIPTS, VENDOR_FILES
from tests.audio_model_fixtures import write_model_fixture


ROOT = Path(__file__).resolve().parents[1]


def package_source(tmp_path):
    shutil.copytree(ROOT / 'extension', tmp_path / 'extension',
                    ignore=shutil.ignore_patterns('vendor', '__pycache__'))
    write_model_fixture(tmp_path / 'extension/audio_model')
    (tmp_path / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/build-extension.sh', tmp_path / 'scripts/build-extension.sh')
    shutil.copytree(ROOT / 'scripts/audio_model', tmp_path / 'scripts/audio_model',
                    ignore=shutil.ignore_patterns('__pycache__'))


@pytest.mark.parametrize("bootstrap", ["current", "legacy_bootstrap", "missing_receiver_option"])
def test_store_package_loads_worker_modules_and_saved_playlist_radio(tmp_path, bootstrap):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the packaged worker smoke check")
    package_source(tmp_path)
    research = tmp_path / 'extension/audio_model/unused-research.bin'
    research.parent.mkdir(exist_ok=True)
    research.write_bytes(b'research assets must never enter the store ZIP')
    if bootstrap != "current":
        background = tmp_path / "extension" / "background.js"
        source = background.read_text().replace("receiverRevision: TC_CONFIG.receiverRevision, ", "")
        if bootstrap == "legacy_bootstrap":
            source = source.replace("revision: TC_CONFIG.sensorRevision, ", "")
        background.write_text(source)
    built = subprocess.run(["bash", str(tmp_path / "scripts" / "build-extension.sh")],
                           capture_output=True, text=True, timeout=20)
    assert built.returncode == 0, built.stderr
    source = json.loads((ROOT / "extension" / "manifest.json").read_text())
    archive, = (tmp_path / "dist").glob("*.zip")
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest == {key: value for key, value in source.items() if key != "key"}
        assert "README.md" not in package.namelist()
        assert not {'tabs', 'history'} & set(manifest['permissions'])
        assert 'audio_model/unused-research.bin' not in package.namelist()
        for name in SCRIPTS:
            assert package.read('audio_model/' + name) == (ROOT / 'extension/audio_model' / name).read_bytes()
        for name in VENDOR_FILES | {'essentia-csp.js', 'bundle.json', 'runtime.json'}:
            assert 'audio_model/vendor/' + name in package.namelist()
        assert json.loads(package.read('audio_model/vendor/bundle.json'))['purpose'] == 'tuneconsole_audio'
        package.extractall(tmp_path / "unpacked")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const root = process.argv[1];
const calls = [], events = [];
const event = () => ({addListener(fn) { events.push(fn); }});
const chrome = {
  runtime: {onMessage:event(), onStartup:event(), onInstalled:event(),
    getURL: name => pathToFileURL(path.join(root, name)).href},
  tabs: {query:async () => [{id:7,url:'https://music.youtube.com/'}],
         sendMessage:async () => ({ok:true,receiverRevision:10,mainVersion:4,mainRevision:10,mainAgeMs:0}),
         onRemoved:event(), onUpdated:event()},
  alarms: {create() {}, onAlarm:event()},
  scripting: {executeScript:async call => {
    calls.push(call);
    return [{result:{ok:true,playback_synced:true}}];
  }},
};
class Socket { static OPEN = 1; static CLOSED = 3; constructor() { this.readyState=0; } }
const {radio, sensors, audioPreview} = require('./tests/extension_worker.cjs').loadWorker({chrome, WebSocket:Socket,
  URL, console, setTimeout() {}, clearTimeout() {}, fetch: async url => ({
    ok: true, json: async () => JSON.parse(fs.readFileSync(new URL(url), 'utf8')),
  })}, root);
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
  const replies = [];
  audioPreview.connect({name:'tc-audio-collection',
    sender:{url:'http://127.0.0.1:8765/',frameId:0},
    onMessage:event(),onDisconnect:event(),postMessage:data=>replies.push(data),
    disconnect(){throw Error('The production collection port must be accepted');}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(replies[0]?.type,'ready','packaged models must enable automatic sampling');
})().catch(error => { console.error(error); process.exitCode=1; });
"""
    result = subprocess.run([node, "-e", script, str(tmp_path / "unpacked")], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('failure', ['missing_models', 'missing_head', 'corrupt_weights',
                                    'corrupt_runtime', 'incomplete_manifest', 'missing_worker'])
def test_store_build_rejects_incomplete_audio_without_replacing_previous_zip(tmp_path, failure):
    package_source(tmp_path)
    model = tmp_path / 'extension/audio_model'
    if failure == 'missing_models':
        shutil.rmtree(model / 'vendor')
    elif failure == 'missing_head':
        (model / 'vendor/happy/model.json').unlink()
    elif failure == 'corrupt_weights':
        (model / 'vendor/musicnn/group1-shard1of1.bin').write_bytes(b'corrupt')
    elif failure == 'corrupt_runtime':
        (model / 'vendor/essentia-csp.js').write_bytes(b'corrupt')
    elif failure == 'missing_worker':
        (model / 'runtime_loader.js').unlink()
    else:
        manifest = model / 'vendor/bundle.json'
        data = json.loads(manifest.read_text())
        data['files'] = [row for row in data['files'] if not row['path'].startswith('sad/')]
        manifest.write_text(json.dumps(data))
    version = json.loads((tmp_path / 'extension/manifest.json').read_text())['version']
    archive = tmp_path / f'dist/tuneconsole-extension-{version}.zip'
    archive.parent.mkdir()
    archive.write_bytes(b'previous verified build')
    result = subprocess.run(['bash', str(tmp_path / 'scripts/build-extension.sh')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert 'Cannot package audio models' in result.stderr
    assert archive.read_bytes() == b'previous verified build'
