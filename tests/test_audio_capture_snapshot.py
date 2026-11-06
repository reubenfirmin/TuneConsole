"""Renamed capture sources must not invalidate frozen research snapshots."""
import hashlib
import json

import pytest

from experiments.audio_lab_tools.audio_evaluation import prepare_current


@pytest.mark.parametrize('names', [
    ('audio_capture.js', 'audio_capture_frame.js', 'audio_capture_frame.html'),
    ('audio_preview_capture.js', 'audio_preview_frame.js', 'audio_preview_frame.html'),
])
def test_capture_snapshot_retains_exact_files_and_matching_manifest(tmp_path, names):
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    contents = {name: f'Frozen capture bytes: {name}\n'.encode() for name in names}
    for name, data in contents.items():
        (snapshot / name).write_bytes(data)
    (snapshot / 'original_manifest.json').write_text(json.dumps({'key': 'frozen-key'}))
    model = snapshot / 'audio_model'
    model.mkdir()
    (model / 'model_worker.js').write_bytes(b'Frozen model worker\n')
    work = tmp_path / 'work'
    work.mkdir()
    extension, hashes, worker = prepare_current(work, snapshot)
    assert {name: (extension / name).read_bytes() for name in names} == contents
    assert hashes == {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    assert worker.read_bytes() == b'Frozen model worker\n'
    manifest = json.loads((extension / 'manifest.json').read_text())
    assert manifest['key'] == 'frozen-key'
    assert manifest['content_scripts'][0]['js'] == [names[0]]
    assert manifest['web_accessible_resources'][0]['resources'] == [names[2]]
    assert {path.name for path in extension.glob('*.js')} == {names[0], names[1], 'audit_background.js'}
