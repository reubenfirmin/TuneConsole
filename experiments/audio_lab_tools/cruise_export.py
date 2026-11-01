"""Package the fixed Cruise expert pair and independently replay its routing."""
import argparse
import base64
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import time
import zlib

import numpy as np
from threadpoolctl import threadpool_limits

from . import candidate_followup as followup
from . import cruise_specialist as runtime
from .external_checkpoint import RUN
from .external_experiment import LAB, digest, save, stamp
from .mapped_vibe_metrics import summary
from .product_baseline_audit import DEFAULT as BASELINE
from .vibe_specialist_round import (EXPERIMENTS, input_arrays, inside, load,
                                    receipt_valid, versions)

DEFAULT = LAB / 'local/mapped-vibes-20261029/cruise-package-1'


def source_replay_errors(expected, actual):
    """Reject source errors even if equal/opposite errors cancel in the ensemble."""
    errors = {}
    for source in runtime.SOURCES:
        before, after = np.asarray(expected[source]), np.asarray(actual[source])
        if (before.ndim != 1 or before.shape != after.shape or not len(before)
                or not np.isfinite(before).all() or not np.isfinite(after).all()):
            raise ValueError('Invalid source replay vectors: ' + source)
        errors[source] = float(np.max(np.abs(before - after)))
        if errors[source] > 1e-10:
            raise ValueError('Cruise source replay differs: ' + source)
    return errors


def captures(signature):
    """Use frozen original excerpt files; no labels or stored predictions scored."""
    from yt_playlist.core import sample_files
    path = RUN / 'inputs/library.json'
    manifest_path = RUN / 'inputs/manifest.json'
    manifest = load(manifest_path)
    library = load(path, manifest['files']['library.json'])
    inputs = {str(path): digest(path), str(manifest_path): digest(manifest_path)}
    grouped, skipped = defaultdict(list), 0
    for row in library['sample_attributes']:
        if not row.get('has_embedding'):
            continue
        sample_path = inside(row['filename'])
        expected = manifest['files']['samples/' + sample_path.name]
        if digest(sample_path) != expected:
            raise ValueError('Frozen capture checksum differs')
        value = sample_files.read(sample_path, expected={k: row[k] for k in ('id', 'run_id', 'point', 'video_id')})
        try:
            compatible = runtime.audio_embedding.signature(value) == signature
        except ValueError:
            compatible = False
        if not compatible:
            skipped += 1
            continue
        inputs[str(sample_path)] = expected
        grouped[row['video_id'], row['run_id']].append(value)
    return grouped, inputs, skipped


def export(directory):
    if directory.exists():
        raise ValueError('Preserve previous Cruise evidence and choose another --directory')
    followup.validate(followup.DEFAULT, execution=False)
    plan = load(followup.DEFAULT / 'plan.json')
    baseline = load(BASELINE / 'plan.json')
    group_path = BASELINE / 'product-model.json'
    if digest(group_path) != baseline['product_sha256']:
        raise ValueError('Frozen shared model changed')
    group = load(group_path)
    captured, captured_inputs, skipped_captures = captures(group['embedding_signature'])
    native, inputs, candidates = {}, {
        str(group_path): digest(group_path),
        str(followup.DEFAULT / 'plan.json'): digest(followup.DEFAULT / 'plan.json'),
        **captured_inputs,
        **{str(followup.DEFAULT / f'inputs/{t}.npz'): plan['inputs'][f'inputs/{t}.npz'] for t in followup.PILOTS}}, {}
    for source in runtime.SOURCES:
        if not receipt_valid(followup.DEFAULT, 'pilots-' + source, digest(followup.DEFAULT / 'plan.json')):
            raise ValueError('Source expert receipt is incomplete')
        path = followup.DEFAULT / f'pilots-{source}.experts.json'
        expert = load(path)['cruise']
        if (expert['family'] != 'embedding' or expert['vibe'] != 'cruise'
                or expert['rule']['expression'] != load(followup.DEFAULT / 'inputs/rules.json')[source]['cruise']['expression']):
            raise ValueError('Frozen Cruise mapping changed')
        inputs[str(path)] = digest(path)
        native[source] = {key: expert[key] for key in ('source', 'target_names', 'model')}
        native[source]['source_expert_sha256'] = digest(path)
        predictions = followup.DEFAULT / f'pilots-{source}.predictions.npz'
        inputs[str(predictions)] = digest(predictions)
        candidates[source] = input_arrays(predictions)
    paths = {str(p.relative_to(EXPERIMENTS)): p for p in
             [Path(__file__), Path(runtime.__file__), EXPERIMENTS / 'run.py',
              Path(__file__).with_name('test_cruise_specialist.py')]}
    paths.update({'product/' + Path(p).name: Path(p) for p in
                  (runtime.production.__file__, runtime.shared.__file__, runtime.audio_embedding.__file__)})
    directory.mkdir(parents=True)
    code = {relative: digest(p) for relative, p in paths.items()}
    for relative, p in paths.items():
        destination = directory / 'source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, destination)
    save(directory / 'plan.json', {'created_at': stamp(), 'code': code, 'runtime': versions(),
         'inputs': inputs, 'source_plan_sha256': digest(followup.DEFAULT / 'plan.json'),
         'shared_model_sha256': digest(group_path), 'fit': False,
         'policy': 'Fixed equal PMEmo/DEAM Cruise; five other scores and shared classification unchanged. No normalization across mixed scales. Shared artifact is the validated product checkpoint, not an unconfirmed post-hoc group winner.',
         'scope': 'compact exact-coefficient artifact; every frozen pilot interval; real frozen capture envelopes and partial captures; no local label scoring or production changes'})
    raw = json.dumps(native, separators=(',', ':'), allow_nan=False).encode()
    artifact = {'format': runtime.FORMAT, 'taxonomy_version': group['taxonomy_version'],
        'feature_version': runtime.audio_embedding.VERSION,
        'feature_names': list(runtime.audio_embedding.FEATURES),
        'embedding_signature': plan['embedding_signature'], 'aggregation': 'mean_native_then_map',
        'combination': 'equal_pmemo_deam', 'score_semantics': 'independent_uncalibrated_suitability',
        'encoding': 'zlib-json-base64', 'payload_sha256': hashlib.sha256(raw).hexdigest(),
        'payload': base64.b64encode(zlib.compress(raw, level=9)).decode(),
        'source_plan_sha256': digest(followup.DEFAULT / 'plan.json')}
    artifact_path = directory / 'cruise-specialist.json'
    artifact_path.write_text(json.dumps(artifact, separators=(',', ':'), allow_nan=False) + '\n')
    shutil.copyfile(group_path, directory / 'shared-model.json')
    before = time.perf_counter()
    model = runtime.load(artifact_path)
    load_seconds = time.perf_counter() - before
    reports, max_error, changed_other_scores, recordings, excerpts = {}, 0., 0, 0, 0
    maximum_source_errors = dict.fromkeys(runtime.SOURCES, 0.)
    probes, timings = [], []
    for target in followup.PILOTS:
        data = input_arrays(followup.DEFAULT / f'inputs/{target}.npz')
        source = followup.DEFAULT / f'inputs/{target}.npz'
        inputs[str(source)] = digest(source)
        observed = candidates['pmemo'][target + '__cruise__observed']
        expected_sources = {s: candidates[s][target + '__mean_native_then_map__cruise__selected'] for s in runtime.SOURCES}
        expected = sum(expected_sources.values()) / 2
        predicted, predicted_sources = [], {source: [] for source in runtime.SOURCES}
        for left, right in zip(data['offsets'][:-1], data['offsets'][1:], strict=True):
            values = data['embedding'][left:right].tolist()
            before = time.perf_counter()
            active = runtime.route(group, values, plan['embedding_signature'], specialist_path=artifact_path, mode='active')
            timings.append(time.perf_counter() - before)
            shadow = runtime.route(group, values, plan['embedding_signature'], specialist_path=artifact_path, mode='shadow')
            for vibe in group['labels']:
                if vibe != 'cruise' and active['suitability'][vibe] != shadow['suitability'][vibe]:
                    changed_other_scores += 1
            if (shadow['suitability'] != shadow['shared_scores']
                    or active['shared_scores'] != shadow['shared_scores']
                    or active['shared_top_vibe'] != shadow['shared_top_vibe']):
                raise ValueError('Cruise changed shared classification or shadow scores')
            predicted.append(active['suitability']['cruise'])
            for source in runtime.SOURCES:
                predicted_sources[source].append(active['individual_models']['cruise']['source_scores'][source])
            if len(probes) < 6:
                probes.append({'one_excerpt': runtime.score(model, values[:1])['score'],
                               'up_to_three_excerpts': runtime.score(model, values[:3])['score'],
                               'available_excerpts': len(values)})
            recordings += 1
            excerpts += len(values)
        predicted = np.asarray(predicted)
        error = float(np.max(np.abs(predicted - expected)))
        source_errors = source_replay_errors(expected_sources, predicted_sources)
        maximum_source_errors = {s: max(maximum_source_errors[s], source_errors[s]) for s in runtime.SOURCES}
        max_error = max(max_error, error)
        reports[target] = {'rows': len(predicted), 'maximum_absolute_replay_error': error,
                           'maximum_source_replay_errors': source_errors,
                           'candidate': summary(observed, predicted, data['outer'])}
    if max_error > 1e-10 or changed_other_scores:
        raise ValueError('Cruise replay or preservation check failed')
    captured_rows, capture_error = 0, 0.
    for samples in captured.values():
        samples.sort(key=lambda row: (row['position_seconds'], row['target_seconds'], row['id']))
        for excerpts_to_use in ([samples[0]], samples):
            reference = runtime.shared.scores(group, excerpts_to_use)
            active = runtime.route_samples(group, excerpts_to_use, specialist_path=artifact_path, mode='active')
            if active['applied_vibes'] != ['cruise']:
                raise ValueError('Compatible real capture did not use Cruise')
            capture_error = max(capture_error, max(abs(reference[v] - active['shared_scores'][v]) for v in group['labels']))
            if any(active['suitability'][v] != reference[v] for v in group['labels'] if v != 'cruise'):
                raise ValueError('Cruise changed another vibe on a real capture')
            captured_rows += 1
    if capture_error > 1e-12 or not captured_rows:
        raise ValueError('Real shared capture replay differs or is absent')
    if any(digest(path) != expected for path, expected in inputs.items()):
        raise ValueError('Frozen Cruise inputs changed during packaging')
    report = {'created_at': stamp(), 'artifact_bytes': artifact_path.stat().st_size,
              'artifact_sha256': digest(artifact_path), 'uncompressed_payload_bytes': len(raw),
              'shared_model_sha256': digest(group_path), 'first_artifact_load_wall_seconds': load_seconds,
              'recordings': recordings, 'excerpts': excerpts, 'maximum_absolute_replay_error': max_error,
              'maximum_source_replay_errors': maximum_source_errors,
              'changed_non_cruise_scores': changed_other_scores, 'shared_classification_unchanged': True,
              'route_wall_seconds': {'median': float(np.median(timings)), 'maximum': max(timings)},
              'timing_scope': 'Elapsed wall time in an already imported interpreter on cached features; excludes fresh-process startup, encoder/extraction and whole-app startup. Not CPU time.',
              'partial_capture_probes': probes, 'cases': reports, 'inputs': inputs,
              'real_capture_replay': {'playback_groups': len(captured), 'one_and_available_excerpt_replays': captured_rows,
                                      'maximum_shared_score_error': capture_error, 'incompatible_excerpt_files': skipped_captures,
                                      'human_labels_scored': False},
              'limits': ['Coefficient/inference checks and reused external ranking, not production deployment or fresh listening acceptance.',
                         'Independent Cruise is for within-vibe ranking; no calibrated threshold or mixed-scale top-vibe choice.',
                         'Other vibes use the validated frozen group model; refreshed/shared alternatives require #570 evidence.']}
    save(directory / 'report.json', report)
    save(directory / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
         'outputs': {name: digest(directory / name) for name in ('cruise-specialist.json', 'shared-model.json', 'report.json')}})
    print(json.dumps({key: report[key] for key in ('artifact_bytes', 'first_artifact_load_wall_seconds', 'recordings', 'excerpts',
          'maximum_absolute_replay_error', 'maximum_source_replay_errors', 'changed_non_cruise_scores', 'route_wall_seconds', 'timing_scope', 'real_capture_replay')}), flush=True)


def verify(directory):
    plan = load(directory / 'plan.json')
    receipt = load(directory / 'receipt.json')
    if receipt['plan_sha256'] != digest(directory / 'plan.json'):
        raise ValueError('Cruise package plan changed')
    for relative, expected in receipt['outputs'].items():
        path = inside(directory / relative)
        if not path.is_relative_to(directory) or digest(path) != expected:
            raise ValueError('Cruise package output changed')
    for relative, expected in plan['code'].items():
        path = inside(directory / 'source' / relative)
        if not path.is_relative_to(directory) or digest(path) != expected:
            raise ValueError('Cruise source archive changed')
    for path, expected in plan['inputs'].items():
        if digest(inside(path)) != expected:
            raise ValueError('Cruise package reference changed')
    print(json.dumps({'directory': str(directory), 'state': 'verified', 'outputs': len(receipt['outputs'])}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'verify'), nargs='?', default='export')
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        (verify if args.action == 'verify' else export)(inside(args.directory))


if __name__ == '__main__':
    main()
