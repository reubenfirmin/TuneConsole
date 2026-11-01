"""Independent, no-fit reconciliation of the frozen #570 research campaign.

Only retained JSON/NPZ and source files are read. Diagnostics go to tmp_path;
there is no application database, audio extraction or bootstrap rerun.
"""
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import test_candidate_followup_review as independent
from yt_playlist.rec import audio_vibe_external


EXPERIMENTS = Path(__file__).resolve().parents[1]
DIRECTORY = EXPERIMENTS / 'audio_lab/local/mapped-vibes-20261029/cruise-baselines-1'
COMMON = DIRECTORY.parent / 'common-target-1'
PLAN = '6167615abd3931f29531ac40295e6f3f7eec8ff0087180e3bd410bcdcf440030'
FIT = 'da5cc32e98487b040d45ba4979947bdf4309db47f360ca0643251aff3a7a5e05'
EVALUATION = '00f971aadf1f9a8ee6a0f63dfa3225a585fb43542a64d8cd5f381254cc028601'
METHODS = ('checkpoint', 'refresh_calibration', 'refresh_both')
VIBES = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')
COHORTS = {'manymusic-natural': (188, 186), 'manymusic-generated': (95, 95),
           'memo2496-pilot': (63, 63), 'soundtracks-all': (470, 470),
           'cal500-raw-activity': (494, 450)}


def _head(stage, values):
    """Direct ridge algebra, without the experiment's replay/predict helpers."""
    standardized = (values - np.asarray(stage['mean'])) / np.asarray(stage['scale'])
    return np.clip(standardized @ np.asarray(stage['weights']) + stage['intercept'], 0, 1)


def _shared(model, values):
    values = np.maximum(_head(model['calibration'], _head(model['native'], values)), 1e-8)
    return values / values.sum(axis=1, keepdims=True)


def test_frozen_baselines_reconcile_all_pairs_and_product_scalar_replay(tmp_path):
    if not DIRECTORY.exists():
        pytest.skip('Separately retained cruise-baselines-1 campaign is unavailable')
    started = time.perf_counter()
    files, checks = {}, {}
    report = {'status': 'incomplete', 'plan_sha256': PLAN, 'checks': checks,
              'runtime': {'python': platform.python_version(), 'numpy': np.__version__},
              'limits': ['No fits, new labels, audio extraction or bootstrap redraws.',
                         'Eligibility masks are reconciled with frozen identity exclusions; unknown aliases remain unknown.',
                         'Reserved results are inspected as saved; reserved outcomes are not rescored.',
                         'Only consumed frozen files are rehashed; this is not a full sample-file verification.',
                         'Research evidence only; no product activation or listening claim.']}

    def read(path, expected=None):
        path = Path(path)
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            assert actual == expected, str(path)
        assert files.setdefault(str(path), actual) == actual, str(path)
        return raw

    def load(path, expected=None):
        return json.loads(read(path, expected))

    def arrays(path, expected):
        read(path, expected)
        with np.load(path, allow_pickle=False) as archive:
            return {key: archive[key] for key in archive.files}

    complete = False
    try:
        # The expected plan and receipts anchor the observed handoff, rather
        # than allowing an altered report to supply its own new fingerprint.
        plan = load(DIRECTORY / 'plan.json', PLAN)
        fit = load(DIRECTORY / 'fit.receipt.json', FIT)
        evaluation = load(DIRECTORY / 'evaluation.receipt.json', EVALUATION)
        assert fit['plan_sha256'] == evaluation['plan_sha256'] == PLAN
        assert evaluation['fit_receipt_sha256'] == FIT
        saved = load(DIRECTORY / 'evaluation.report.json', evaluation['outputs']['evaluation.report.json'])
        fit_report = load(DIRECTORY / 'fit.report.json', fit['outputs']['fit.report.json'])
        predictions = arrays(DIRECTORY / 'external-predictions.npz', evaluation['outputs']['external-predictions.npz'])
        assert saved['plan_sha256'] == PLAN and fit_report['reserved_outcomes_read'] is False
        assert plan['created_at'] < fit_report['created_at'] < saved['created_at']
        assert plan['bootstrap_draws'] == 1000 and plan['bootstrap_seed'] == 571570
        for relative, expected in plan['code'].items():
            read(DIRECTORY / 'source' / relative, expected)
        product_path = Path(audio_vibe_external.__file__).resolve()
        assert product_path == EXPERIMENTS.parent / 'src/yt_playlist/rec/audio_vibe_external.py'
        read(product_path, plan['code']['product/audio_vibe_external.py'])
        read(Path(__file__))
        read(Path(independent.__file__))

        # The numerical replay kernel omitted from this campaign's direct code
        # list is recoverable through its pinned package -> common-plan chain.
        package = DIRECTORY.parent / 'cruise-package-2'
        package_receipt = load(package / 'receipt.json', plan['references'][str(package / 'receipt.json')])
        package_plan = load(package / 'plan.json', package_receipt['plan_sha256'])
        common_plan = load(COMMON / 'plan.json', package_plan['inputs'][str(COMMON / 'plan.json')])
        for name in ('mapped_vibe_inference.py', 'vibe_specialist_round.py'):
            relative = 'audio_lab_tools/' + name
            read(COMMON / 'source' / relative, common_plan['code'][relative])
            read(EXPERIMENTS / relative, common_plan['code'][relative])
        checks['indirect_numerical_source_chain'] = True

        development = load(DIRECTORY / 'inputs/development.json', plan['inputs']['inputs/development.json'])
        reserved = load(DIRECTORY / 'inputs/reserved.json', plan['inputs']['inputs/reserved.json'])
        inventory = load(DIRECTORY / 'inputs/inventory.json', plan['inputs']['inputs/inventory.json'])
        native = load(DIRECTORY / 'inputs/native.json', plan['inputs']['inputs/native.json'])
        native_arrays = arrays(DIRECTORY / 'inputs/native.npz', plan['inputs']['inputs/native.npz'])
        prior_path = next(Path(path) for path in plan['references'] if path.endswith('/all-sources-1/inputs/local.json'))
        prior = load(prior_path, plan['references'][str(prior_path)])
        dev_ids = {row['video_id'] for row in development['rows']}
        held_ids = {row['video_id'] for row in reserved['rows']}
        assert len(development['rows']) == len(dev_ids) == inventory['development_rows'] == 76
        assert len(reserved['rows']) == len(held_ids) == inventory['reserved_rows'] == 8
        assert dev_ids.isdisjoint(held_ids) and held_ids == set(inventory['new_ids'])
        assert dev_ids == {row['video_id'] for row in prior['protected_identities']}
        assert len(dev_ids | held_ids) == inventory['usable_rows'] == 84
        assert set(development['groups']).isdisjoint(reserved['groups'])
        assert inventory['label_revisions_since_original_gold'] == []
        assert not any(held_ids & set(item['matched_ids']) for item in inventory['exposure'].values())
        assert len(native['rows']) == len(native['groups']) == len(native_arrays['embedding']) == 260
        assert len(native_arrays['y']) == native['exclusions']['training_rows'] == 260
        assert len(native['exclusions']['excluded_ids']) == 140
        later_exposure = load(DIRECTORY / 'shared-native-exposure.json')
        assert later_exposure['plan_sha256'] == PLAN and later_exposure['evaluation_receipt_sha256'] == EVALUATION
        assert later_exposure['native_rows'] == 400 and later_exposure['reserved_rows'] == 8
        assert later_exposure['matched_reserved_ids'] == [] and later_exposure['outcomes_scored'] is False
        for path, expected in later_exposure['inputs'].items():
            read(Path(path), expected)
        checks['cohorts'] = {'development': 76, 'reserved': 8, 'native': 260, 'native_excluded': 140}

        models = {name: load(DIRECTORY / ('checkpoint.json' if name == 'checkpoint' else name + '.json'),
                             plan['artifacts']['checkpoint.json'] if name == 'checkpoint'
                             else fit['outputs'][name + '.json']) for name in METHODS}
        assert models['refresh_calibration']['native'] == models['checkpoint']['native']
        scalar_errors = {}
        x = np.asarray([row['embedding_values'] for row in development['rows']])
        assert x.shape == (76, 200) and np.isfinite(x).all()
        with threadpool_limits(limits=1):
            for name, model in models.items():
                audio_vibe_external.validate(model)
                assert model['labels'] == list(VIBES)
                manual = _shared(model, x)
                actual = []
                for values in x:
                    emotions = audio_vibe_external._predict(model['native'], values.tolist())
                    raw = [max(value, 1e-8) for value in audio_vibe_external._predict(model['calibration'], emotions)]
                    actual.append([value / sum(raw) for value in raw])
                scalar_errors[name] = float(np.max(abs(manual - actual)))
                np.testing.assert_allclose(actual, manual, atol=1e-12, rtol=0, err_msg=name)
            checks['scalar_replay'] = {'development_rows_per_model': 76, 'max_absolute_error': scalar_errors}

            assert set(saved['cases']) == set(plan['external_targets']) == set(COHORTS)
            summaries, pairs, intervals, score_errors = {}, 0, 0, {}
            expected_keys = set()
            for target, (original_count, retained_count) in COHORTS.items():
                metadata_path, data_path = COMMON / f'inputs/{target}.json', COMMON / f'inputs/{target}.npz'
                metadata = load(metadata_path, plan['references'][str(metadata_path)])
                data = arrays(data_path, plan['references'][str(data_path)])
                case = saved['cases'][target]
                keep = predictions[target + '__retained']
                expected_keys.add(target + '__retained')
                assert keep.dtype.kind == 'b' and keep.shape == (original_count,)
                assert int(keep.sum()) == case['shared_training_disjoint_rows'] == retained_count
                assert len(metadata['rows']) == len(metadata['groups']) == len(data['y']) == case['rows'] == original_count
                assert len({row['id'] for row in metadata['rows']}) == original_count
                assert [row['id'] for row, retained in zip(metadata['rows'], keep, strict=True) if not retained] == case['exclusion']['excluded_ids']
                assert case['exclusion']['training_rows'] == retained_count
                groups = np.asarray(metadata['groups'])
                assert all(len(set(data['outer'][groups == group])) == 1 for group in set(groups))
                if 'offsets' in data:
                    offsets = data['offsets']
                    assert len(offsets) == original_count + 1 and offsets[0] == 0
                    assert offsets[-1] == len(data['embedding']) and (np.diff(offsets) > 0).all()
                    features = np.asarray([data['embedding'][left:right].mean(axis=0)
                                           for left, right in zip(offsets[:-1], offsets[1:], strict=True)])
                else:
                    features = data['embedding']
                control_scores = {name: _shared(model, features) for name, model in models.items()}
                rules = plan['annotation_rules']['pmemo' if 'offsets' in data else target]
                supported = set(rules) & set(VIBES)
                assert set(case['vibes']) == supported
                summaries[target], score_errors[target] = {}, {}
                for vibe in supported:
                    pairs += 1
                    entry = case['vibes'][vibe]
                    observed = independent._reference(rules[vibe]['expression'], data['y'], metadata['target_names'])
                    key = target + '__' + vibe + '__observed'
                    expected_keys.add(key)
                    np.testing.assert_allclose(predictions[key], observed, atol=1e-15, rtol=0)
                    expected_methods = set(METHODS) | ({'cruise_specialist'} if 'offsets' in data and vibe == 'cruise' else set())
                    assert set(entry['metrics']) == expected_methods
                    calculated = {}
                    for name in expected_methods:
                        key = target + '__' + vibe + '__' + name
                        expected_keys.add(key)
                        score = predictions[key]
                        assert score.shape == observed.shape and np.isfinite(score).all()
                        if name in METHODS:
                            manual = control_scores[name][:, VIBES.index(vibe)]
                            score_errors[target][vibe + '/' + name] = float(np.max(abs(score - manual)))
                            np.testing.assert_allclose(score, manual, atol=1e-12, rtol=0, err_msg=key)
                        calculated[name] = independent._metrics(observed[keep], score[keep], data['outer'][keep])
                        independent._equal(entry['metrics'][name], calculated[name], key)
                    expected_pairs = {name + '_vs_checkpoint': (name, 'checkpoint') for name in expected_methods - {'checkpoint'}}
                    if 'cruise_specialist' in expected_methods:
                        expected_pairs.update({'cruise_specialist_vs_' + name: ('cruise_specialist', name) for name in METHODS[1:]})
                    assert set(entry['paired']) == set(expected_pairs)
                    for name, (candidate, baseline) in expected_pairs.items():
                        interval = entry['paired'][name]
                        assert interval['ndcg_gain'] == pytest.approx(calculated[candidate]['ndcg'] - calculated[baseline]['ndcg'], abs=1e-12)
                        assert interval['draws'] == interval['usable_draws'] == 1000
                        bounds = np.asarray(interval['conditional_95_percent_interval'])
                        assert bounds.shape == (2,) and np.isfinite(bounds).all() and bounds[0] <= bounds[1]
                        assert 'identity-group' in interval['policy'] and 'No multiple-comparison correction' in interval['policy']
                        intervals += 1
                    summaries[target][vibe] = calculated
            assert pairs == 20 and len(expected_keys) == len(predictions) and set(predictions) == expected_keys
            assert sum(len(methods) for vibes in summaries.values() for methods in vibes.values()) == 63
            checks.update(supported_pairs=pairs, metric_summaries=63, interval_point_checks=intervals,
                          metrics=summaries, external_control_max_absolute_errors=score_errors)

        for slice_name, local in saved['local_supplement'].items():
            assert local['rows'] == 8, slice_name
            assert set(local['methods']) == set(METHODS) | {name + '_with_cruise' for name in METHODS}
            for name in METHODS:
                base, hybrid = local['methods'][name], local['methods'][name + '_with_cruise']
                assert 'shared_top_choice' in base and 'shared_top_choice' not in hybrid
                assert base['shared_top_choice']['top_1_acceptable'] == .75
                for vibe in set(VIBES) - {'cruise'}:
                    assert base['per_vibe'][vibe] == hybrid['per_vibe'][vibe], (slice_name, name, vibe)
        checks['saved_reserved_other_five_metrics_unchanged'] = True
        complete = True
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        unchanged = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
                     for path, expected in files.items()}
        report.update(status='passed' if complete and all(unchanged.values()) else 'failed_or_incomplete',
                      elapsed_seconds=time.perf_counter() - started, consumed_files=files, unchanged=unchanged)
        (tmp_path / 'cruise-baseline-independent-review.json').write_text(json.dumps(report, indent=2) + '\n')
        assert all(unchanged.values()), unchanged
