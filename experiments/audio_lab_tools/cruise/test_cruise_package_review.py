"""No-fit independent replay of the frozen #571 package, not #573 integration.

Read frozen coefficients, supplied embeddings and capture envelopes only. Never
open local human labels or regenerate artifacts; write diagnostics to tmp_path.
"""
import builtins
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import platform
import time
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from experiments.audio_lab_tools.studies import test_candidate_followup_review as oracle


EXPERIMENTS = Path(__file__).resolve().parents[2]
CAMPAIGN = EXPERIMENTS / 'audio_lab/local/mapped-vibes-20261029'
DIRECTORY = CAMPAIGN / 'cruise-package-3'
COMMON = CAMPAIGN / 'common-target-1'
CAPTURE = EXPERIMENTS / 'audio_lab/local/external-20261028/checkpoint-1/inputs'
PLAN = 'b895235f5df946493100c209c7a9557ec6329da885aac57b4013208cd37f1145'
RECEIPT = '7811e05df6e31bf0ea0355ce5d2b58cf906a1c57795e8b0a3b3eb7528baf7e27'
ARTIFACT = '3927a3294ad7a1670ef7f81d5c6119943991a699de60a7d5a60022851ba304eb'
# The oracle's package/root paths changed; its numerical implementation is unchanged.
ORACLE = 'd80e2b82e7fef0ccb6199c9665ce532062fa3bb04574446b5947ce1dabcba2ef'
COHORTS = {'manymusic-natural': (188, 564), 'manymusic-generated': (95, 285),
           'memo2496-pilot': (63, 378)}
LABELS = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')


def _shared(model, embedding):
    """Direct two-stage matrix algebra, independent of the scalar runtime."""
    def head(stage, values):
        return np.clip(((values - stage['mean']) / stage['scale'])
                       @ np.asarray(stage['weights']) + stage['intercept'], 0, 1)
    values = np.maximum(head(model['calibration'], head(model['native'], embedding)), 1e-8)
    return values / values.sum()


def test_frozen_compact_package_replays_each_source_and_real_capture_envelopes(tmp_path):
    if not DIRECTORY.exists():
        pytest.skip('Separately retained cruise-package-3 is unavailable')
    started = time.perf_counter()
    files, checks, timings = {}, {}, []
    report = {'status': 'incomplete', 'plan_sha256': PLAN, 'checks': checks,
              'runtime': {'python': platform.python_version(), 'numpy': np.__version__},
              'limits': ['Exact archived package-3 runtime, embedding validator and shared scorer execute privately.',
                         'The taxonomy constant is explicitly fixed to audio-session-targets-1.',
                         'No current #573 product runtime is imported or accepted by this audit.',
                         'No fitting, extraction, bootstrap redraw or local human-label reads.',
                         'Only supplied capture envelopes are replayed; original library selection is not repeated.',
                         'Source expert file identities are reconciled with the pinned plan, not re-exported.',
                         'Elapsed timings exclude extraction, process and application startup.',
                         'Research inference evidence; no product activation or listening-purpose claim.']}

    def read(path, expected=None):
        path = Path(path).resolve()
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            assert actual == expected, str(path)
        assert files.setdefault(str(path), actual) == actual
        return raw

    def load(path, expected=None):
        return json.loads(read(path, expected))

    def private(name, relative, bindings):
        """Execute exact archived bytes without pyc writes or advancing imports."""
        path = DIRECTORY / 'source' / relative
        raw = read(path, plan['code'][relative])
        module = ModuleType(name)
        module.__file__ = str(path)

        def imports(name, globals=None, locals=None, fromlist=(), level=0):
            if name in bindings:
                return bindings[name]
            assert not name.startswith('yt_playlist'), name
            return builtins.__import__(name, globals, locals, fromlist, level)

        module.__dict__['__builtins__'] = {**vars(builtins), '__import__': imports}
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        return module

    complete = False
    try:
        plan = load(DIRECTORY / 'plan.json', PLAN)
        receipt = load(DIRECTORY / 'receipt.json', RECEIPT)
        assert receipt['plan_sha256'] == PLAN and plan['fit'] is False
        saved = load(DIRECTORY / 'report.json', receipt['outputs']['report.json'])
        assert plan['created_at'] < saved['created_at'] and saved['inputs'] == plan['inputs']
        for relative, expected in plan['code'].items():
            read(DIRECTORY / 'source' / relative, expected)
        read(Path(__file__))
        assert Path(oracle.__file__).resolve() == EXPERIMENTS / 'audio_lab_tools/studies/test_candidate_followup_review.py'
        read(oracle.__file__, ORACLE)
        checks['archived_sources'] = {relative: plan['code'][relative] for relative in plan['code']}
        executing = (Path(__file__).resolve(), Path(oracle.__file__).resolve(),
                     DIRECTORY / 'source/product/audio_embedding.py',
                     DIRECTORY / 'source/product/audio_vibe_external.py',
                     DIRECTORY / 'source/audio_lab_tools/cruise_specialist.py')
        checks['executed_sources'] = {str(path): files[str(path)] for path in executing}
        taxonomy = SimpleNamespace(TAXONOMY_VERSION='audio-session-targets-1')
        embedding = private('review571_embedding', 'product/audio_embedding.py', {})
        shared = private('review571_shared', 'product/audio_vibe_external.py', {
            'yt_playlist.core': SimpleNamespace(audio_embedding=embedding),
            'yt_playlist.core.audio_vibe_contract': taxonomy})
        runtime = private('review571_cruise', 'audio_lab_tools/cruise_specialist.py', {
            'yt_playlist.core': SimpleNamespace(audio_embedding=embedding),
            'yt_playlist.core.audio_vibe_contract': taxonomy,
            'yt_playlist.rec': SimpleNamespace(audio_vibe_external=shared)})
        assert runtime.audio_embedding is embedding and runtime.shared is shared
        assert shared.audio_embedding is embedding
        artifact_path = DIRECTORY / 'cruise-specialist.json'
        raw = read(artifact_path, ARTIFACT)
        assert len(raw) == saved['artifact_bytes'] == 187217
        assert receipt['outputs']['cruise-specialist.json'] == saved['artifact_sha256'] == ARTIFACT
        group = load(DIRECTORY / 'shared-model.json', receipt['outputs']['shared-model.json'])
        assert group['labels'] == list(LABELS) and group['taxonomy_version'] == taxonomy.TAXONOMY_VERSION
        assert saved['shared_model_sha256'] == plan['shared_model_sha256'] == receipt['outputs']['shared-model.json']
        common = load(COMMON / 'plan.json', plan['inputs'][str(COMMON / 'plan.json')])
        before = time.perf_counter()
        model = runtime.load(artifact_path)
        checks['first_archived_load_wall_seconds'] = time.perf_counter() - before
        assert model['sha256'] == ARTIFACT
        assert model['header']['source_plan_sha256'] == plan['source_plan_sha256'] == files[str(COMMON / 'plan.json')]
        assert model['header']['embedding_signature'] == group['embedding_signature'] == common['embedding_signature']
        predictions = {}
        for source in runtime.SOURCES:
            expert_path = COMMON / f'pilots-{source}.experts.json'
            assert model['experts'][source]['source_expert_sha256'] == plan['inputs'][str(expert_path)]
            path = COMMON / f'pilots-{source}.predictions.npz'
            read(path, plan['inputs'][str(path)])
            with np.load(path, allow_pickle=False) as archive:
                predictions[source] = {target: archive[f'{target}__mean_native_then_map__cruise__selected']
                                       for target in COHORTS}
        assert {model['experts'][s]['model']['kind'] for s in runtime.SOURCES} == {'ridge', 'rff'}
        oracle_models = {}
        for source, expert in model['experts'].items():
            head = expert['model']
            if head['kind'] == 'rff':
                # fit_path serializes a plain ridge head here. Only the older
                # independent oracle requires its redundant discriminator.
                linear = head['linear']
                assert set(linear) == {'medians', 'mean', 'scale', 'weights', 'intercept', 'alpha'}
                assert np.isfinite(linear['alpha']) and linear['alpha'] > 0
                oracle_models[source] = {**head, 'linear': {'kind': 'ridge', **linear}}
            else:
                oracle_models[source] = head
        checks['oracle_layout_bridge'] = 'Oracle-only RFF inner ridge kind; original coefficient objects unchanged.'
        errors = dict.fromkeys(runtime.SOURCES, 0.)
        native_error = shared_error = combined_error = 0.

        def check_route(values, active, native_expected):
            nonlocal native_error, shared_error
            assert active['applied_vibes'] == ['cruise']
            candidate = active['individual_models']['cruise']
            assert candidate['sample_count'] == len(values) and candidate['sha256'] == ARTIFACT
            source_scores = []
            for source, expert in model['experts'].items():
                expected = native_expected[source]
                actual = np.asarray([candidate['native'][source][key] for key in expert['target_names']])
                native_error = max(native_error, float(np.max(abs(actual - expected))))
                np.testing.assert_allclose(actual, expected, atol=1e-12, rtol=0)
                axes = dict(zip(expert['target_names'], expected, strict=True))
                score = (1 - 2 * abs(axes['arousal'] - .5) + axes['valence']) / 2
                assert abs(candidate['source_scores'][source] - score) <= 1e-12
                source_scores.append(score)
            assert abs(candidate['score'] - np.mean(source_scores)) <= 1e-12
            assert active['suitability']['cruise'] == candidate['score']
            expected_shared = _shared(group, np.asarray(values).mean(axis=0))
            actual_shared = np.asarray([active['shared_scores'][v] for v in LABELS])
            shared_error = max(shared_error, float(np.max(abs(actual_shared - expected_shared))))
            np.testing.assert_allclose(actual_shared, expected_shared, atol=1e-12, rtol=0)
            off = runtime.route(group, values, group['embedding_signature'], mode='off')
            assert active['shared_scores'] == off['shared_scores'] == off['suitability']
            assert active['shared_top_vibe'] == off['shared_top_vibe'] == LABELS[int(np.argmax(expected_shared))]
            assert all(active['suitability'][v] == off['suitability'][v] for v in LABELS if v != 'cruise')
            return candidate

        rows = excerpts = 0
        for target, (row_count, excerpt_count) in COHORTS.items():
            path = COMMON / f'inputs/{target}.npz'
            read(path, plan['inputs'][str(path)])
            with np.load(path, allow_pickle=False) as archive:
                values, offsets = archive['embedding'], archive['offsets']
            assert values.shape == (excerpt_count, 200) and np.isfinite(values).all()
            assert offsets.shape == (row_count + 1,) and offsets.dtype.kind in 'iu'
            assert offsets[0] == 0 and offsets[-1] == excerpt_count and np.all(np.diff(offsets) > 0)
            native = {s: oracle._manual_head(head, values) for s, head in oracle_models.items()}
            actual_sources, actual_combined = {s: [] for s in runtime.SOURCES}, []
            for left, right in zip(offsets[:-1], offsets[1:], strict=True):
                chunks = values[left:right].tolist()
                before = time.perf_counter()
                active = runtime.route(group, chunks, group['embedding_signature'], specialist_path=artifact_path, mode='active')
                timings.append(time.perf_counter() - before)
                candidate = check_route(chunks, active, {s: a[left:right].mean(axis=0) for s, a in native.items()})
                for source in runtime.SOURCES:
                    actual_sources[source].append(candidate['source_scores'][source])
                actual_combined.append(candidate['score'])
            expected_combined = np.zeros(row_count)
            for source in runtime.SOURCES:
                expected = predictions[source][target]
                assert expected.shape == (row_count,) and np.isfinite(expected).all()
                errors[source] = max(errors[source], float(np.max(abs(actual_sources[source] - expected))))
                np.testing.assert_allclose(actual_sources[source], expected, atol=1e-12, rtol=0)
                expected_combined += expected / 2
            combined_error = max(combined_error, float(np.max(abs(actual_combined - expected_combined))))
            np.testing.assert_allclose(actual_combined, expected_combined, atol=1e-12, rtol=0)
            assert saved['cases'][target]['rows'] == row_count
            rows += row_count
            excerpts += excerpt_count
        assert (rows, excerpts) == (saved['recordings'], saved['excerpts']) == (346, 1227)

        manifest_path = CAPTURE / 'manifest.json'
        manifest = load(manifest_path, plan['inputs'][str(manifest_path)])
        grouped, sample_ids = defaultdict(list), set()
        for filename, expected in plan['inputs'].items():
            path = Path(filename)
            if path.parent != CAPTURE / 'samples':
                continue
            assert path.stem == expected == manifest['files']['samples/' + path.name]
            sample_raw = read(path, expected)
            assert len(sample_raw) <= 256 * 1024
            sample = json.loads(sample_raw)
            assert all(isinstance(sample.get(k), str) and sample[k] for k in ('id', 'video_id', 'run_id', 'point'))
            assert sample['id'] not in sample_ids
            sample_ids.add(sample['id'])
            assert embedding.signature(sample) == group['embedding_signature']
            assert all(np.isfinite(sample[k]) and sample[k] >= 0 for k in ('position_seconds', 'target_seconds'))
            grouped[sample['video_id'], sample['run_id']].append(sample)
        capture_cases = 0
        for samples in grouped.values():
            samples.sort(key=lambda s: (s['position_seconds'], s['target_seconds'], s['id']))
            for selected in ([samples[0]], samples):
                values = np.asarray([s['embedding']['values'] for s in selected])
                native = {s: oracle._manual_head(head, values).mean(axis=0) for s, head in oracle_models.items()}
                active = runtime.route_samples(group, selected, specialist_path=artifact_path, mode='active')
                check_route(values.tolist(), active, native)
                capture_cases += 1
        assert len(grouped) == saved['real_capture_replay']['playback_groups'] == 93
        assert capture_cases == saved['real_capture_replay']['one_and_available_excerpt_replays'] == 186
        assert len(sample_ids) == 256 and saved['real_capture_replay']['human_labels_scored'] is False
        assert saved['changed_non_cruise_scores'] == 0 and saved['shared_classification_unchanged'] is True
        checks.update(pilot_rows=rows, supplied_excerpts=excerpts, maximum_source_errors=errors,
                      maximum_combined_error=combined_error, maximum_native_error=native_error,
                      maximum_shared_error=shared_error, supplied_capture_files=len(sample_ids),
                      capture_groups=len(grouped), capture_cases=capture_cases,
                      route_wall_seconds={'median': float(np.median(timings)), 'maximum': max(timings)})
        complete = True
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        unchanged = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
                     for path, expected in files.items()}
        report.update(status='passed' if complete and all(unchanged.values()) else 'failed_or_incomplete',
                      elapsed_seconds=time.perf_counter() - started, consumed_files=files, unchanged=unchanged)
        (tmp_path / 'cruise-package-independent-review.json').write_text(json.dumps(report, indent=2) + '\n')
        assert all(unchanged.values()), unchanged
