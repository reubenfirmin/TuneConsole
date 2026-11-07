"""Before/after replay of frozen real captures for the #702 runtime extraction.

Run test_freeze before changing production, then test_replay afterwards. Evidence
is retained outside disposable pytest fixtures; existing baselines cannot be replaced.
"""
import hashlib
import json
from pathlib import Path

import pytest

from experiments.audio_lab_tools.cruise import cruise_specialist as routing
from experiments.audio_lab_tools.focus.focus_export import captures
from yt_playlist.core import features
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release
from yt_playlist.rec import audio_vibe_relax as relax, audio_vibe_uplift as uplift
from yt_playlist.rec import audio_vibe_energize as energize, audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor

MODULES = (cruise, release, relax, uplift, energize, focus)
DIRECTORY = Path(__file__).resolve().parents[1] / 'audio_lab/local/specialist-cleanup-702-20261101'
BASELINE = DIRECTORY / 'baseline.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def observe(monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / 'absent.env')
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, str(shared.DEFAULT_PATH))
    for module in MODULES:
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
        monkeypatch.setenv(module.MODE_KEY, 'active')
    group = json.loads(shared.DEFAULT_PATH.read_bytes())
    recordings = captures()
    cases, routes = [], []
    for metadata, samples in recordings:
        for count in sorted({1, min(3, len(samples)), len(samples)}):
            rows = samples[:count]
            cases.append({'video_id': metadata['video_id'], 'count': count,
                          'samples_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
                          'prediction': predictor.predict(rows, require_complete=False)})
            routes.append([routing.route_samples(group, rows, specialist_path=cruise.DEFAULT_PATH, mode=mode)
                           for mode in ('off', 'shadow', 'active')])
    rows = recordings[0][1]
    failures = {}
    for module in MODULES:
        for failure in ('off', 'missing'):
            with monkeypatch.context() as isolated:
                if failure == 'off':
                    isolated.setenv(module.MODE_KEY, 'off')
                else:
                    isolated.setenv(module.ENVIRONMENT_KEY, str(tmp_path / 'missing.json'))
                failures[module.VERSION + '/' + failure] = predictor.predict(rows, require_complete=False)
    with monkeypatch.context() as isolated:
        isolated.setenv(cruise.MODE_KEY, 'shadow')
        failures['cruise/shadow'] = predictor.predict(rows, require_complete=False)
    return {'artifacts': {m.DEFAULT_PATH.name: digest(m.DEFAULT_PATH) for m in (*MODULES, shared)},
            'identities': [m.identity() for m in MODULES], 'recordings': len(recordings),
            'partial_recordings': sum(len(rows) < 3 for _, rows in recordings),
            'cases': cases, 'routes': routes, 'failure_and_shadow_cases': failures}


@pytest.mark.skipif(BASELINE.exists(), reason='The immutable pre-extraction baseline is already recorded')
def test_freeze(monkeypatch, tmp_path):
    assert not BASELINE.exists(), 'Preserve the original pre-extraction baseline'
    result = observe(monkeypatch, tmp_path)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    (DIRECTORY / 'before-sources.json').write_text(json.dumps(
        {str(m.__file__): digest(m.__file__) for m in (*MODULES, routing)}, indent=2) + '\n')
    print({'recordings': result['recordings'], 'cases': len(result['cases']),
           'route_cases': 3 * len(result['routes']), 'baseline_sha256': digest(BASELINE)})


def test_replay(monkeypatch, tmp_path):
    expected = json.loads(BASELINE.read_bytes())
    actual = observe(monkeypatch, tmp_path)
    assert actual == expected, 'Runtime extraction changed frozen real-capture outputs'
    report = {'ticket': 702, 'recordings': actual['recordings'], 'partial_recordings': actual['partial_recordings'],
              'composed_cases': len(actual['cases']), 'research_route_cases': 3 * len(actual['routes']),
              'failure_and_shadow_cases': len(actual['failure_and_shadow_cases']),
              'baseline_sha256': digest(BASELINE), 'exact_equality': True,
              'artifacts': actual['artifacts'], 'fit': False}
    (DIRECTORY / 'replay.json').write_text(json.dumps(report, indent=2) + '\n')
    print(report)
