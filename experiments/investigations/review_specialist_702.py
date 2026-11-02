"""Reviewer replay of receipt-checked recordings against the frozen pre-extraction output."""
import hashlib
import json
from pathlib import Path

from experiments.audio_lab_tools import cruise_specialist as routing
from experiments.audio_lab_tools.focus_export import captures
from yt_playlist.core import features
from yt_playlist.rec import audio_vibe_runtime as runtime
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release
from yt_playlist.rec import audio_vibe_relax as relax, audio_vibe_uplift as uplift
from yt_playlist.rec import audio_vibe_energize as energize, audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_preview as preview

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path('/tmp/tuneconsole-review-specialists-702-20261102')
BASELINE = ROOT/'experiments/audio_lab/local/specialist-cleanup-702-20261101/baseline.json'
BASELINE_SHA = 'e63865b297046dd715d150a2bb3ae1147eff015a223102a5145f6214bfc37c34'
SPECIALISTS = (cruise, release, relax, uplift, energize, focus)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_real_capture_output_and_research_adapter_parity(monkeypatch, tmp_path):
    assert digest(BASELINE) == BASELINE_SHA
    expected = json.loads(BASELINE.read_bytes())
    paths = [Path(module.__file__) for module in (*SPECIALISTS, shared, preview, runtime, routing)]
    paths += [module.DEFAULT_PATH for module in (*SPECIALISTS, shared)]
    before = {str(path.relative_to(ROOT)): digest(path) for path in paths}
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path/'absent.env')
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, str(shared.DEFAULT_PATH))
    for module in SPECIALISTS:
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
        monkeypatch.setenv(module.MODE_KEY, 'active')
    group = json.loads(shared.DEFAULT_PATH.read_bytes())
    recordings = captures()
    assert len(recordings) == expected['recordings'] == 84
    assert sum(len(samples) < 3 for _, samples in recordings) == expected['partial_recordings'] == 11
    assert {module.DEFAULT_PATH.name: digest(module.DEFAULT_PATH) for module in (*SPECIALISTS, shared)} == expected['artifacts']
    position = 0
    for metadata, samples in recordings:
        for count in sorted({1, min(3, len(samples)), len(samples)}):
            rows = samples[:count]
            case = expected['cases'][position]
            assert (case['video_id'], case['count']) == (metadata['video_id'], count)
            assert hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest() == case['samples_sha256']
            assert preview.predict(rows, require_complete=False) == case['prediction']
            for mode, wanted in zip(('off', 'shadow', 'active'), expected['routes'][position], strict=True):
                assert routing.route_samples(group, rows, specialist_path=cruise.DEFAULT_PATH, mode=mode) == wanted
            position += 1
    assert position == len(expected['cases']) == 159
    assert {str(path.relative_to(ROOT)): digest(path) for path in paths} == before
    assert digest(BASELINE) == BASELINE_SHA
    (REPORT/'real-capture-replay.json').write_text(json.dumps({
        'baseline_sha256': BASELINE_SHA, 'baseline_origin': 'author pre-extraction observation',
        'recordings': 84, 'partial_recordings': 11, 'composed_cases': position,
        'research_routing_cases': position * 3, 'exact_output_equality': True,
        'sources_and_artifacts_unchanged': before, 'fit': False,
    }, indent=2))
