"""Freeze, execute and verify the two controlled Focus experiment series."""
import argparse
from pathlib import Path
import shutil
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import focus_controls as models, focus_listeners as listeners
from ..studies import individual_protocol as protocol
from ..support.external_experiment import DATA, LAB, digest, save, stamp
from .focus_experiment import arrays, check_receipt, inside, load, runtime
from ..studies.individual_experiment import compact, receipt
from ..relax.relax_experiment import validate
from ..uplift.uplift_consensus import subset

EXPERIMENTS = LAB.parent
PREVIOUS = LAB / 'local/focus-615-20261030/round-1'
REMAINING = LAB / 'local/individual-20261030/remaining-1'
DISCOVERY = DATA / 'discovery/focus-576'
DEFAULT = LAB / 'local/focus-controls-637-638-20261030/round-1'


def freeze(directory):
    if directory.exists():
        raise ValueError('Preserve prior attempts; choose a new directory')
    validate(PREVIOUS)
    check_receipt(PREVIOUS, 'evaluation.receipt.json')
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    ref(PREVIOUS / 'plan.json')
    ref(PREVIOUS / 'evaluation.receipt.json')
    for f in range(5):
        check_receipt(PREVIOUS, f'outer-{f}.receipt.json')
        ref(PREVIOUS / f'outer-{f}.npz')
    directory.mkdir(parents=True)
    (directory / 'inputs').mkdir()
    for source in (models.CAL, models.EMO):
        d = arrays(ref(PREVIOUS / f'inputs/{source}.npz'))
        if source == models.EMO:
            original = arrays(ref(REMAINING / f'inputs/{source}.npz'))
            meta = load(ref(REMAINING / f'inputs/{source}.json'))
            ids = [r['id'] for r in meta['rows']]
            positions = [ids.index(i) for i in d['ids']]
            if not np.array_equal(original['embedding'][positions], d['x']):
                raise ValueError('Emotify alignment changed')
            d['y'] = original['y'][positions][:, [meta['target_names'].index(k) for k in models.EMOTIONS]]
        np.savez_compressed(directory / f'inputs/{source}.npz', **d)
    meta = load(ref(PREVIOUS / f'inputs/{models.CAL}.json'))
    cal = arrays(directory / f'inputs/{models.CAL}.npz')
    ref(DATA / 'cal500/annotations-source/README_CAL500.txt')
    records = listeners.cal_responses(meta['rows'], DATA / 'cal500/annotations-source', ref)
    observed = np.array([[np.mean([r[k] for r in records if r['recording'] == i])
                          for k in ('reading', 'studying')] for i in range(len(cal['y']))])
    if not np.allclose(observed, cal['y'], rtol=0, atol=1e-12):
        raise ValueError('Individual votes do not reproduce frozen targets')
    compact(directory / 'inputs/cal-responses.json', records)
    moods, moods_audit = listeners.moods_input(ref(DISCOVERY / 'moods-activities/Data.zip'))
    ref(DISCOVERY / 'moods-activities/ReadMe.txt')
    np.savez_compressed(directory / 'inputs/moods.npz', **moods)
    ref(DISCOVERY / 'study/index.xlsx')
    study = listeners.helpstudy(ref(DISCOVERY / 'study/esm_raw_data.xlsx'))
    save(directory / 'inputs/audit.json', {'moods': moods_audit, 'helpstudy': study,
        'cal': {'rows': len(cal['y']), 'common': int(cal['common'].sum()), 'responses': len(records),
                'groups': len(set(cal['groups'])), 'uid_responses': sum(bool(r['uid']) for r in records)}})
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(m, '__file__', None)}
    paths.update((EXPERIMENTS / 'run.py',
                  Path(__file__).with_name('test_focus_controls.py')))
    code = {}
    for path in sorted(paths):
        name = str(path.relative_to(EXPERIMENTS))
        destination = directory / 'source' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        code[name] = digest(path)
    save(directory / 'plan.json', {'created_at': stamp(), 'tickets': [637, 638],
        'runtime': runtime(), 'references': references, 'code': code,
        'inputs': {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').iterdir()},
        'methods': models.METHODS, 'predecessor': str(PREVIOUS),
        'exposure': 'Reused development populations; no fresh confirmation or production adoption.',
        'terms': 'Research only; retain original CAL500, Emotify, Moods and Activities and HelpStudy terms.'})
    validate(directory, True)
    print({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
           'audit': load(directory / 'inputs/audit.json')}, flush=True)


def fit_all(directory):
    data = {s: arrays(directory / f'inputs/{s}.npz') for s in (models.CAL, models.EMO)}
    d = data[models.CAL]
    for fold in range(5):
        stage = f'outer-{fold}'
        if (directory / f'{stage}.receipt.json').exists():
            check_receipt(directory, f'{stage}.receipt.json')
            continue
        start = time.monotonic()
        valid = d['outer'] == fold
        training = models.protect(data, d['groups'][valid])
        print({'stage': stage, 'rows': {s: len(v['y']) for s, v in training.items()}}, flush=True)
        model, trace = models.fit(training)
        out = {'ids': d['ids'][valid]}
        for prep, x in (('historical', d['x'][valid]), ('browser', d['browser'][valid, 0])):
            out.update({prep + '__' + k: v for k, v in models.predict(model, x).items()})
        paths = [directory / f'models/{stage}.json', directory / f'{stage}.trace.json', directory / f'{stage}.npz']
        compact(paths[0], model)
        compact(paths[1], trace)
        np.savez_compressed(paths[2], **out)
        receipt(directory, stage, paths)
        print({'completed': stage, 'seconds': time.monotonic() - start}, flush=True)


def listener_stage(directory):
    if (directory / 'listeners.receipt.json').exists():
        check_receipt(directory, 'listeners.receipt.json')
        return
    cal = arrays(directory / f'inputs/{models.CAL}.npz')
    print({'stage': 'same-song associations'}, flush=True)
    associations = listeners.associations(load(directory / 'inputs/cal-responses.json'), cal['groups'])
    print({'stage': 'repeated-listener experiments'}, flush=True)
    moods, predictions = listeners.evaluate_moods(arrays(directory / 'inputs/moods.npz'))
    paths = [directory / 'listeners.json', directory / 'listeners.npz']
    compact(paths[0], {'cal': associations, 'moods': moods, 'audit': load(directory / 'inputs/audit.json')})
    np.savez_compressed(paths[1], **predictions)
    receipt(directory, 'listeners', paths)
    print({'completed': 'listeners'}, flush=True)


def verify_trace(trace, outer_groups):
    forbidden = set(outer_groups)
    checks = 0

    def check_native(native_trace, excluded):
        nonlocal checks
        if set(native_trace['training_groups']) & excluded:
            raise ValueError('Validation component in native training')
        protocol.group_guard(native_trace['training_groups'], native_trace['inner_folds'])
        checks += 1

    if forbidden & set(trace['training_groups']):
        raise ValueError('Outer group in map training')
    for block in trace['selection'].values():
        inner_forbidden = forbidden | set(block['validation_groups'])
        if set(block['training_groups']) & inner_forbidden:
            raise ValueError('Inner group in map training')
        check_native(block['native_trace'], inner_forbidden)
        for cross in block['crossfit'].values():
            check_native(cross['selection'], inner_forbidden | set(cross['validation_groups']))
    check_native(trace['final_native'], forbidden)
    for cross in trace['final_crossfit'].values():
        check_native(cross['selection'], forbidden | set(cross['validation_groups']))
    return checks


def task_interval(d, scores, prep):
    delta = ((scores[prep + '__direct'] - d['y']) ** 2 - (scores[prep + '__task_offset'] - d['y']) ** 2).mean(1)
    rng, draws = np.random.default_rng(63830), []
    blocks = [[np.flatnonzero(d['groups'] == g) for g in sorted(set(d['groups'][d['outer'] == f]))] for f in range(5)]
    for _ in range(1000):
        sample = np.concatenate([np.concatenate([b[i] for i in rng.integers(len(b), size=len(b))]) for b in blocks])
        draws.append(delta[sample].mean())
    return {'difference': float(delta.mean()), 'conditional_95_percent_interval': np.quantile(draws, [.025, .975]).tolist()}


def evaluate(directory):
    d = arrays(directory / f'inputs/{models.CAL}.npz')
    protocol.group_guard(d['groups'], d['outer'])
    scores, audit = {}, {'nested_native_checks': 0, 'replay_max_abs': 0., 'direct_previous_max_abs': 0., 'folds': []}
    for fold in range(5):
        stage = f'outer-{fold}'
        check_receipt(directory, stage + '.receipt.json')
        out, model, trace = arrays(directory / f'{stage}.npz'), load(directory / f'models/{stage}.json'), load(directory / f'{stage}.trace.json')
        valid = d['outer'] == fold
        if not np.array_equal(out['ids'], d['ids'][valid]):
            raise ValueError('OOF identity mismatch')
        audit['nested_native_checks'] += verify_trace(trace, d['groups'][valid])
        previous = arrays(PREVIOUS / f'{stage}.npz')
        rank = model['native']['rank']
        q = np.asarray(model['native']['basis'])
        w = models.slopes(model['native']['heads'], 200)
        fold_audit = {'fold': fold, 'native_rank': rank, 'map_selection': trace['map_selection'], 'preparations': {}}
        for prep, x in (('historical', d['x'][valid]), ('browser', d['browser'][valid, 0])):
            replay = models.predict(model, x, independent=True)
            raw = models.raw_heads(model['native']['heads'], x)
            # Independent full projector verifies the compact residual transform.
            residual = (x - model['native']['mean']) @ (np.eye(200) - q @ q.T)
            if not np.allclose(residual, models.representation(model['native'], x)['residual'], atol=1e-10):
                raise ValueError('Residual projection replay failed')
            orthogonal = float(np.abs(residual @ w).max())
            if orthogonal > 1e-9:
                raise ValueError('Residual retains native linear emotion slope')
            fold_audit['preparations'][prep] = {'clipped_fraction': float(((raw < 0) | (raw > 1)).mean()),
                                               'residual_slope_max_abs': orthogonal}
            for method in models.METHODS:
                key = prep + '__' + method
                scores.setdefault(key, np.empty_like(d['y']))[valid] = out[key]
                audit['replay_max_abs'] = max(audit['replay_max_abs'], float(np.abs(out[key] - replay[method]).max()))
            audit['direct_previous_max_abs'] = max(audit['direct_previous_max_abs'],
                float(np.abs(out[prep + '__direct'] - previous[prep + '__direct']).max()))
        audit['folds'].append(fold_audit)
    if audit['replay_max_abs'] > 1e-12 or audit['direct_previous_max_abs'] > 1e-12:
        raise ValueError('Model replay or exact direct baseline differs')
    paths = [directory / 'evaluation.json', directory / 'oof.npz']
    np.savez_compressed(paths[1], **scores)
    common = d['common'].astype(bool)
    dd = subset(d, common)
    cc = {k: v[common] for k, v in scores.items()}
    reports = {}
    pairs = [('emotion_residual', 'emotion'), ('residual', 'mean'), ('direct', 'emotion'),
             ('pca9', 'emotion'), ('random9', 'emotion'), ('emotion_raw', 'emotion'),
             ('direct', 'task_offset'), ('task_offset', 'task_pooled')]
    for prep in ('historical', 'browser'):
        reports[prep] = {'task_mse_direct_vs_offset': task_interval(dd, cc, prep)}
        for column, target in ((None, 'focus'), (0, 'reading'), (1, 'studying')):
            print({'evaluating': prep, 'target': target}, flush=True)
            y = dd['y'].mean(1) if column is None else dd['y'][:, column]
            predictions = {m: cc[prep + '__' + m].mean(1) if column is None else cc[prep + '__' + m][:, column] for m in models.METHODS}
            reports[prep][target] = {'metrics': {m: protocol.metrics(y, p, dd['outer']) for m, p in predictions.items()},
                'paired': protocol.paired(y, predictions, dd['outer'], dd['groups'], pairs, draws=1000)}
    compact(paths[0], {'rows': int(common.sum()), 'audit': audit, 'results': reports})
    receipt(directory, 'evaluation', paths)
    print({'completed': 'evaluation', 'audit': {k: v for k, v in audit.items() if k != 'folds'}}, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'fit', 'listeners', 'evaluate', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
        else:
            validate(directory, execution=args.action != 'verify')
            if args.action == 'fit':
                fit_all(directory)
            elif args.action == 'listeners':
                listener_stage(directory)
            elif args.action == 'evaluate':
                evaluate(directory)
            else:
                for stage in ('outer-0', 'outer-1', 'outer-2', 'outer-3', 'outer-4', 'listeners', 'evaluation'):
                    check_receipt(directory, stage + '.receipt.json')
                print({'verified': str(directory), 'plan_sha256': digest(directory / 'plan.json')}, flush=True)


if __name__ == '__main__':
    main()
