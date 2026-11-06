"""Artifact storage and failure isolation shared by all six specialists."""
from copy import deepcopy
import hashlib
import json
import os

import pytest

from tests.test_audio_vibe_cruise import samples
from yt_playlist.core import fao, features
from yt_playlist.rec import audio_vibe_runtime as runtime
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release
from yt_playlist.rec import audio_vibe_relax as relax, audio_vibe_uplift as uplift
from yt_playlist.rec import audio_vibe_energize as energize, audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor

SPECIALISTS = (cruise, release, relax, uplift, energize, focus)
VIBES = ('cruise', 'release', 'relax', 'uplift', 'energize', 'concentrate')


@pytest.mark.parametrize('module', SPECIALISTS, ids=VIBES)
@pytest.mark.parametrize('compressed', [False, True])
def test_logical_artifact_digest_and_cache_follow_replacement(module, compressed, tmp_path):
    raw = module.DEFAULT_PATH.read_bytes()
    path = tmp_path / module.DEFAULT_PATH.name
    if compressed:
        fao.write_bytes(path, raw)
    else:
        path.write_bytes(raw)
    first = module.load(path)
    assert first['sha256'] == hashlib.sha256(raw).hexdigest()
    assert module.load(path) is first
    modified = path.stat().st_mtime_ns
    header = json.loads(raw)
    old = header['source_plan_sha256']
    new = 'a' * 64 if old != 'a' * 64 else 'b' * 64
    changed = raw.replace(old.encode(), new.encode(), 1)
    assert len(changed) == len(raw)
    if compressed:
        fao.write_bytes(path, changed)
    else:
        replacement = tmp_path / 'replacement.json'
        replacement.write_bytes(changed)
        os.utime(replacement, ns=(modified, modified))
        replacement.replace(path)
    second = module.load(path)
    assert second['sha256'] == hashlib.sha256(changed).hexdigest() != first['sha256']
    assert second['header']['source_plan_sha256'] == new


@pytest.mark.parametrize('module,vibe', list(zip(SPECIALISTS, VIBES, strict=True)), ids=VIBES)
@pytest.mark.parametrize('error_type', runtime.MODEL_ERRORS)
def test_one_specialist_failure_preserves_other_five(module, vibe, error_type, monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / 'absent.env')
    for item in (shared, *SPECIALISTS):
        monkeypatch.setenv(item.ENVIRONMENT_KEY, str(item.DEFAULT_PATH))
    for item in SPECIALISTS:
        monkeypatch.setenv(item.MODE_KEY, 'active')
    rows = samples()
    original = deepcopy(rows)
    before = predictor.predict(rows)
    assert set(before['individual_models']) == set(VIBES)

    def fail(_path):
        raise error_type('Unavailable optional model')

    monkeypatch.setattr(module, 'load', fail)
    after = predictor.predict(rows)
    assert set(after['individual_models']) == set(VIBES) - {vibe}
    assert {k: v for k, v in after.items() if k not in ('suitability', 'individual_models')} == {
        k: v for k, v in before.items() if k not in ('suitability', 'individual_models')}
    for other in set(VIBES) - {vibe}:
        assert after['individual_models'][other] == before['individual_models'][other]
        assert after['suitability'][other] == before['suitability'][other]
    assert after['suitability'][vibe] == next(row['score'] for row in before['ranking'] if row['vibe'] == vibe)
    assert rows == original


@pytest.mark.parametrize('module', SPECIALISTS, ids=VIBES)
def test_explicit_empty_environment_overrides_local_model_and_mode(module, monkeypatch, tmp_path):
    local = tmp_path / '.env.local'
    local.write_text(f'{module.ENVIRONMENT_KEY}="{module.DEFAULT_PATH}"\n{module.MODE_KEY}=active\n')
    monkeypatch.setattr(features, 'LOCAL_ENV', local)
    monkeypatch.delenv(module.ENVIRONMENT_KEY, raising=False)
    monkeypatch.delenv(module.MODE_KEY, raising=False)
    assert module.configured_path() == str(module.DEFAULT_PATH)
    assert module.mode() == 'active'
    monkeypatch.setenv(module.ENVIRONMENT_KEY, '')
    monkeypatch.setenv(module.MODE_KEY, '')
    assert module.configured_path() is None
    assert module.mode() == 'off'
    assert module.identity() == module.VERSION + '/off/shared'


def test_read_bounds_apply_to_logical_bytes_and_changed_lengths(tmp_path):
    path = tmp_path / 'artifact.json'
    raw = b'{"header": {"padding": "' + b'x' * 1024 + b'"}}'
    fao.write_bytes(path, raw)
    assert path.stat().st_size < len(raw)
    with pytest.raises(ValueError):
        runtime.read_artifact(path, len(raw), 512, lambda value: value)
    with pytest.raises(ValueError):
        runtime.read_artifact(path, len(raw) - 1, 2048, lambda value: value)
    with pytest.raises(ValueError):
        runtime.read_artifact(path, 0, 2048, lambda value: value)
    # Preserve Cruise's historical bounded read when the size changed.
    assert runtime.read_artifact(path, len(raw) - 1, 2048, lambda value: value,
                                 exact_size=False)['sha256'] == hashlib.sha256(raw).hexdigest()
