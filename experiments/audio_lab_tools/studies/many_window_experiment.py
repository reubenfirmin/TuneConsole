"""Freeze/extract/fit/evaluate #707 without changing application code."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
from threading import Event
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import many_window_models as models, individual_protocol as protocol
from ..support import quality_round as evidence, quality_baselines as baselines
from ..datasets.dataset_windows import many_music_rows
from ..support.external_experiment import DATA, LAB, digest, save, stamp
from ..training.external_training import folds
from ..focus.focus_experiment import arrays, check_receipt, inside, load
from .individual_experiment import compact
from .mapped_vibe_rules import apply, registry
from ..audio.quality_reference_audio import Extractor, provenance, GRAPH, PACKAGE
from ..relax.relax_experiment import global_groups

DEFAULT = LAB / 'local/many-windows-707-20261101/round-5'
PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
KINDS = ('natural', 'generated')
VIBES = ('cruise', 'relax', 'energize', 'uplift')
VA = ('valence', 'arousal')


def freeze(directory):
    root = DATA / 'discovery/manymusic'
    inputs = {'windows.json': root / 'human-window-index.json', 'credits.json': root / 'audio-provenance-index.json'}
    rows = load(inputs['windows.json'])['rows']
    credits = {r['recording_id']: r for r in load(inputs['credits.json'])['rows']}
    selected = {r['id'] for r in many_music_rows(rows)}
    decorated = []
    for row in rows:
        credit = credits.get(row['recording_id'])
        if credit is None and not row['synthetic_audio']:
            raise ValueError('Natural recording lacks verified attribution')
        if (credit is not None and row['audio_sha256'] != credit['audio_sha256']) or row['annotation_seconds'] != 30:
            raise ValueError('Window attribution/audio identity differs')
        decorated.append({**row, **({k: credit[k] for k in ('artist', 'title')} if credit else {}), 'selected': row['id'] in selected})
    reference_rows, metadata = [], {}
    reference_inputs = LAB / 'local/per-vibe-20261029/all-sources-1/inputs'
    for path in sorted(reference_inputs.glob('*.json')):
        meta = load(path)
        if 'rows' in meta:
            inputs['identities/' + path.name] = path
            reference_rows.extend(meta['rows'])
    for source in ('manymusic-natural', 'manymusic-generated', 'memo2496-pilot', 'soundtracks-all'):
        inputs[source + '.npz'] = PREVIOUS / f'inputs/{source}.npz'
        inputs[source + '.json'] = PREVIOUS / f'inputs/{source}.json'
        metadata[source] = load(inputs[source + '.json'])
        reference_rows.extend(metadata[source]['rows'])
    local = LAB / 'local/individual-20261030/local-1/labels.json'
    inputs['local-identities.json'] = local
    local_rows = load(local)['rows']
    all_rows = [*decorated, *reference_rows, *local_rows]
    groups = global_groups(all_rows)
    protected = set(groups[len(all_rows) - len(local_rows):])
    prepared = []
    for row, group in zip(decorated, groups, strict=False):
        prepared.append({**row, 'component': int(group), 'local_disjoint': group not in protected})
    # Retain explicit component IDs for each transfer/native input, independent of old numbering.
    metadata_start = len(decorated) + len(reference_rows) - sum(len(m['rows']) for m in metadata.values())
    components = {}
    for source, meta in metadata.items():
        components[source] = groups[metadata_start:metadata_start + len(meta['rows'])].tolist()
        metadata_start += len(meta['rows'])
    for vibe in VIBES:
        inputs['adopted-' + vibe + '.json'] = LAB.parent.parent / f'src/yt_playlist/rec/data/{vibe}-vibes.json'
    extraction = provenance()
    refs = [Path(r['audio']) for r in {r['audio']: r for r in rows}.values()]
    refs += [GRAPH, PACKAGE / 'METADATA', PACKAGE / 'RECORD']
    refs += [LAB / f'local/many-windows-707-20261101/round-{n}/plan.json' for n in range(1, 5)]
    plan = evidence.freeze(directory, 707, {'selected_window_rule': 'new-audio-pilot-1',
        'alphas': models.ALPHAS, 'extraction': extraction, 'bootstrap_draws': 1000,
        'budgets': {'extraction_seconds': 7200, 'workers': 2, 'feature_bytes': 1024 ** 3, 'fit_seconds': 7200, 'rss_bytes': 1024 ** 3},
        'resource_revision': 'Earlier Node attempts preserved before any fits. Round 5 uses the previously accepted #424 C++ MusiCNN path uniformly for every window, with two single-thread workers. Before fitting, every selected-window embedding must agree with the original within the existing .0005 feature tolerance. No Node chunks are mixed into the new training features.',
        'eligibility_revision_before_fits': 'The metadata-only audit finds natural recordings have 5-10 windows and all 95 generated recordings have exactly one. Require meaningful additional-window gain on natural music; generated music is an unchanged negative control, never an additional-window gain claim. No fitted or evaluation outcomes inspected before this correction.',
        'native_targets': list(VA), 'vibes': VIBES, 'components': 'All raw windows, thirteen-source inventories, prior pilot/transfer identities and local rows.'},
        inputs, references=refs, documents=[Path(__file__).with_name('test_many_windows.py')])
    save(directory / 'inputs/window-plan.json', {'rows': prepared, 'components': components,
        'audit': {'rated_windows': len(rows), 'recordings': len({r['recording_id'] for r in rows}),
                  'selected_windows': len(selected), 'known_local_components': len(protected),
                  'natural_generated_windows': dict(Counter('generated' if r['synthetic_audio'] else 'natural' for r in rows)),
                  'response_policy': load(inputs['windows.json'])['policy']}})
    plan['inputs']['inputs/window-plan.json'] = digest(directory / 'inputs/window-plan.json')
    save(directory / 'plan.json', plan)
    evidence.validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'windows': len(rows), 'selected': len(selected), 'plan_sha256': digest(directory / 'plan.json')}), flush=True)


def extract(directory):
    plan = evidence.validate(directory, True)
    if (directory / 'extraction.receipt.json').exists():
        check_receipt(directory, 'extraction.receipt.json')
        return
    rows = load(directory / 'inputs/window-plan.json')['rows']
    retained, errors, chunk_paths, resources = [], [], set(), []
    chunks, completed = [], {}
    workers = plan['protocol']['budgets']['workers']
    stopped = Event()

    def branch(number):
        workspace = directory / f'worker-{number}'
        workspace.mkdir(exist_ok=True)
        with Extractor(workspace, plan['protocol']['budgets']['extraction_seconds']) as worker:
            for index in range(number, len(rows), workers):
                if stopped.is_set():
                    break
                row, values, paths = rows[index], [], []
                try:
                    for offset in (0, 10, 20):
                        vector, path = worker.chunk(row, row['annotation_start'] + offset)
                        values.append(vector)
                        paths.append(path)
                    completed[index] = (values, paths, None)
                except (ValueError, OSError) as error:
                    # Coverage failures reject intervals; provenance/runtime/budget failures stop the run.
                    if 'cover' not in str(error):
                        raise
                    completed[index] = (None, [], str(error))
        return worker.resources

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(branch, n) for n in range(workers)]
        last = -1
        try:
            while not all(f.done() for f in pending):
                for future in pending:
                    if future.done() and future.exception():
                        raise future.exception()
                count = len(completed)
                if count // 25 != last:
                    last = count // 25
                    save(directory / 'extraction-status.json', {'updated_at': stamp(), 'processed': count, 'total': len(rows),
                        'errors': [rows[i]['id'] for i, result in completed.items() if result[2]]})
                    print(json.dumps({'windows': count, 'total': len(rows)}), flush=True)
                time.sleep(1)
            resources = [f.result() for f in pending]
        finally:
            stopped.set()
    for index, row in enumerate(rows):
        values, paths, error = completed[index]
        if error:
            errors.append({'id': row['id'], 'error': error})
        else:
            retained.append(row)
            chunks.append(values)
            chunk_paths.update(paths)
    if sum(p.stat().st_size for p in chunk_paths) > plan['protocol']['budgets']['feature_bytes']:
        raise ValueError('Frozen feature-storage budget exceeded')
    # Independently establish the accepted numerical contract before fitting.
    delta = 0.
    for kind in KINDS:
        old = arrays(directory / f'inputs/manymusic-{kind}.npz')
        old_ids = [r['id'] for r in load(directory / f'inputs/manymusic-{kind}.json')['rows']]
        for row, values in zip(retained, chunks, strict=True):
            if row['selected'] and row['id'] in old_ids:
                index = old_ids.index(row['id'])
                start, stop = old['offsets'][index:index + 2]
                delta = max(delta, float(np.max(np.abs(np.asarray(values) - old['embedding'][start:stop]))))
    if delta > .0005:
        raise RuntimeError('Reference extraction differs from accepted MusiCNN feature tolerance')
    np.savez_compressed(directory / 'windows.npz', chunks=np.asarray(chunks), x=np.asarray(chunks).mean(1),
        ids=np.asarray([r['id'] for r in retained]), recordings=np.asarray([r['recording_id'] for r in retained]),
        y=np.asarray([[(r['targets'][r['target_names'].index(k)] + 1) / 2 for k in VA] for r in retained]),
        groups=np.asarray([r['component'] for r in retained]), selected=np.asarray([r['selected'] for r in retained]),
        synthetic=np.asarray([r['synthetic_audio'] for r in retained]), local_disjoint=np.asarray([r['local_disjoint'] for r in retained]))
    save(directory / 'extraction.json', {'rows': retained, 'rejected': errors, 'resources': {'workers': resources,
        'elapsed_seconds': max(r['elapsed_seconds'] for r in resources), 'new_chunks': sum(r['new_chunks'] for r in resources),
        'reused_chunks': sum(r['reused_chunks'] for r in resources)},
        'chunks': len(chunk_paths), 'feature_bytes': sum(p.stat().st_size for p in chunk_paths),
        'selected_window_pilot_maximum_feature_error': delta,
        'chunk_labels': 'One thirty-second target per mean embedding; no independently labeled ten-second chunks.'})
    evidence.seal(directory, 'extraction', [directory / 'windows.npz', directory / 'extraction.json', *sorted(chunk_paths)])


def native_predict(model, chunks):
    return models.predict(model, chunks.reshape(-1, 200)).reshape(*chunks.shape[:2], 2).mean(1)


def metric_block(y, scores, outer, groups):
    return {'metrics': {m: protocol.metrics(y, p, outer) for m, p in scores.items()},
        'paired': protocol.paired(y, scores, outer, groups, [('all', 'one'), ('all', 'adopted')], 1000)}


def run(directory):
    plan = evidence.validate(directory, True)
    check_receipt(directory, 'extraction.receipt.json')
    d = arrays(directory / 'windows.npz')
    meta = load(directory / 'inputs/window-plan.json')
    started = time.monotonic()
    report, outputs, paths = {'created_at': stamp(), 'native': {}, 'transfer': {}}, {}, []
    for kind, synthetic in (('natural', False), ('generated', True)):
        eligible = (d['synthetic'] == synthetic) & d['local_disjoint']
        assignment = folds(d['groups'][eligible], count=5, seed=70701)
        protocol.group_guard(d['groups'][eligible], assignment)
        full_assignment = np.full(len(d['y']), -1)
        full_assignment[eligible] = assignment
        evaluated = eligible & d['selected']
        source = 'manymusic-' + kind
        old = arrays(directory / f'inputs/{source}.npz')
        oldmeta = load(directory / f'inputs/{source}.json')
        oldids = [r['id'] for r in oldmeta['rows']]
        selected_ids = d['ids'][evaluated].tolist()
        positions = np.asarray([oldids.index(i) for i in selected_ids])
        # Match the incumbent disjoint/native comparison mask, without hiding rejected intervals.
        common = old['common'][positions]
        evaluated[np.flatnonzero(evaluated)[~common]] = False
        y, groups, outer = d['y'][evaluated], d['groups'][evaluated], full_assignment[evaluated]
        pooled = {prep + '__' + name: np.empty((sum(evaluated), 2)) for prep in ('historical', 'browser') for name in ('one', 'all')}
        valid_locations = np.flatnonzero(evaluated)
        for fold in range(5):
            stage = kind + f'-outer-{fold}'
            train, valid = eligible & (full_assignment != fold), evaluated & (full_assignment == fold)
            fitted, trace = {}, {}
            for name in ('one', 'all'):
                fitted[name], trace[name] = models.select(d, train, name == 'all')
                forbidden = set(d['groups'][valid])
                if forbidden & set(trace[name]['fit_groups']):
                    raise ValueError('Held-out component in native fit')
            paths2 = [directory / f'{stage}.model.json', directory / f'{stage}.trace.json']
            compact(paths2[0], fitted)
            compact(paths2[1], trace)
            evidence.seal(directory, stage, paths2)
            paths.extend(paths2)
            index = np.isin(valid_locations, np.flatnonzero(valid))
            browser_indices = np.asarray([oldids.index(i) for i in d['ids'][valid]])
            browser = old['browser'].reshape(len(oldids), 3, 200)[browser_indices]
            for prep, chunks in (('historical', d['chunks'][valid]), ('browser', browser)):
                for name in ('one', 'all'):
                    pooled[prep + '__' + name][index] = native_predict(fitted[name], chunks)
            print(json.dumps({'native': kind, 'fold': fold, 'recordings': int(sum(valid))}), flush=True)
        positions = np.asarray([oldids.index(i) for i in d['ids'][evaluated]])
        results = {}
        for prep in ('historical', 'browser'):
            chunks = d['chunks'][evaluated] if prep == 'historical' else old['browser'].reshape(len(oldids), 3, 200)[positions]
            native = {name: pooled[prep + '__' + name] for name in ('one', 'all')}
            mapped = {}
            for vibe in VIBES:
                expression = registry()['deam'][vibe]['expression']
                observed = apply(expression, y, VA)
                scores = {name: apply(expression, p, VA) for name, p in native.items()}
                scores['adopted'] = baselines.predict(load(directory / f'inputs/adopted-{vibe}.json'), chunks, np.full(len(chunks), 3))[0]
                mapped[vibe] = metric_block(observed, scores, outer, groups)
                prefix = kind + '__' + prep + '__' + vibe + '__'
                outputs.update({prefix + name: p for name, p in scores.items()})
                outputs.update({prefix + 'observed': observed, prefix + 'outer': outer, prefix + 'groups': groups})
            results[prep] = {'rows': len(y), 'groups': len(set(groups)), 'mapped': mapped,
                'native': {name: {'mse': ((p - y) ** 2).mean(0).tolist(), 'mae': np.abs(p - y).mean(0).tolist()} for name, p in native.items()}}
            outputs.update({kind + '__' + prep + '__native__' + name: p for name, p in native.items()})
        report['native'][kind] = results
        for destination in ('soundtracks-all', 'memo2496-pilot'):
            target = arrays(directory / f'inputs/{destination}.npz')
            targetmeta = load(directory / f'inputs/{destination}.json')
            allgroups = np.asarray(meta['components'][destination])
            forbidden = set(allgroups)
            train = eligible & np.asarray([g not in forbidden for g in d['groups']])
            fitted, traces = {}, {}
            for name in ('one', 'all'):
                fitted[name], traces[name] = models.select(d, train, name == 'all')
                if forbidden & set(traces[name]['fit_groups']):
                    raise ValueError('Target component in transfer fit')
            stage = kind + '-transfer-' + destination
            paths2 = [directory / f'{stage}.model.json', directory / f'{stage}.trace.json']
            compact(paths2[0], fitted)
            compact(paths2[1], traces)
            evidence.seal(directory, stage, paths2)
            paths.extend(paths2)
            keep = target['common']
            group, outer = allgroups[keep], folds(allgroups[keep], count=5, seed=70701)
            number = len(target['y'])
            chunk_count = 1 if destination == 'soundtracks-all' else 6
            for prep, key in (('historical', 'embedding'), ('browser', 'browser')):
                chunks = target[key].reshape(number, chunk_count, 200)[keep]
                predicted = {name: native_predict(model, chunks) for name, model in fitted.items()}
                mapped = {}
                for vibe in VIBES:
                    expression = registry()['deam' if destination == 'memo2496-pilot' else destination][vibe]['expression']
                    observed = apply(expression, target['y'][keep], targetmeta['target_names'])
                    scores = {name: apply(registry()['deam'][vibe]['expression'], p, VA) for name, p in predicted.items()}
                    scores['adopted'] = baselines.predict(load(directory / f'inputs/adopted-{vibe}.json'), chunks, np.full(len(chunks), chunk_count))[0]
                    mapped[vibe] = metric_block(observed, scores, outer, group)
                    prefix = kind + '__' + destination + '__' + prep + '__' + vibe + '__'
                    outputs.update({prefix + name: p for name, p in scores.items()})
                    outputs.update({prefix + 'observed': observed, prefix + 'outer': outer, prefix + 'groups': group})
                report['transfer'][kind + '__' + destination + '__' + prep] = {'rows': sum(keep).item(), 'mapped': mapped,
                    'native': {name: {'mse': ((p - target['y'][keep][:, [targetmeta['target_names'].index(k) for k in VA]]) ** 2).mean(0).tolist()}
                        for name, p in predicted.items()} if destination == 'memo2496-pilot' else None}
    report['extraction'] = load(directory / 'extraction.json')['resources']
    report['resources'] = {'fit_evaluation_seconds': time.monotonic() - started,
        'python_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        'model_bytes_max': max(p.stat().st_size for p in paths if p.name.endswith('.model.json'))}
    if report['resources']['python_peak_rss_bytes'] > plan['protocol']['budgets']['rss_bytes'] or time.monotonic() - started > 7200:
        raise ValueError('Fit/evaluation resource bound exceeded')
    # The predeclared average-gain test is necessary; individual cells cannot select a winner.
    native_points, regression_checks = [], []
    for kind, result in report['native'].items():
        for prep, block in result.items():
            prefix = kind + '__' + prep + '__'
            outer, groups = outputs[prefix + 'cruise__outer'], outputs[prefix + 'cruise__groups']
            blocks = [[np.flatnonzero((groups == g) & (outer == f)) for g in sorted(set(groups[outer == f]))]
                      for f in sorted(set(outer))]
            rng, draws = np.random.default_rng(70701), []
            for _ in range(1000):
                ids = np.concatenate([np.concatenate([b[i] for i in rng.integers(len(b), size=len(b))]) for b in blocks])
                delta = [protocol.brief_metrics(outputs[prefix + v + '__observed'][ids], outputs[prefix + v + '__all'][ids], outer[ids]) -
                         protocol.brief_metrics(outputs[prefix + v + '__observed'][ids], outputs[prefix + v + '__one'][ids], outer[ids]) for v in VIBES]
                draws.append(np.mean(delta, axis=0))
            interval = np.quantile(draws, [.025, .975], axis=0).T.tolist()
            native_points.append({'kind': kind, 'preparation': prep,
                'mean_ndcg_gain': float(np.mean([m['paired']['all__vs__one']['ndcg']['difference'] for m in block['mapped'].values()])),
                'mean_lift_gain': float(np.mean([m['paired']['all__vs__one']['top_lift']['difference'] for m in block['mapped'].values()])),
                'joint_component_95_percent_intervals': interval})
            regression_checks.extend([m['paired']['all__vs__one'][metric]['difference'] >= -.01
                for m in block['mapped'].values() for metric in ('ndcg', 'top_lift')])
            regression_checks.append(max(np.asarray(block['native']['all']['mse']) - block['native']['one']['mse']) <= .005)
    necessary = all(p['mean_ndcg_gain'] >= .02 and p['mean_lift_gain'] >= .01 and p['joint_component_95_percent_intervals'][0][0] > 0
                    for p in native_points if p['kind'] == 'natural')
    generated_control = all(abs(p['mean_ndcg_gain']) <= 1e-12 and abs(p['mean_lift_gain']) <= 1e-12
                            for p in native_points if p['kind'] == 'generated')
    production_checks = []
    for block in report['transfer'].values():
        for m in block['mapped'].values():
            regression_checks.extend([m['paired']['all__vs__one'][metric]['difference'] >= -.01 for metric in ('ndcg', 'top_lift')])
            production_checks.extend([m['paired']['all__vs__adopted']['ndcg']['difference'] >= .02,
                                      m['paired']['all__vs__adopted']['top_lift']['difference'] >= .01])
    report['decision'] = {'necessary_native_gain_checks_pass': necessary, 'native_points': native_points,
        'single_window_generated_negative_control_unchanged': generated_control,
        'regression_checks_pass': all(regression_checks), 'production_controls_pass': all(production_checks),
        'conclusion': 'recommend' if necessary and generated_control and all(regression_checks) and all(production_checks) else 'retain',
        'meaning': 'A VA-only expert comparison; fixed adopted mixture weights and production artifacts remain unchanged.'}
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **outputs)
    evidence.seal(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz'])
    print(json.dumps({'decision': report['decision'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'extract', 'run', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
        elif args.action == 'extract':
            extract(directory)
        elif args.action == 'run':
            run(directory)
        else:
            print(json.dumps(evidence.verify(directory)))


if __name__ == '__main__':
    main()
