"""Supersede only transfers affected by transitive source identity exclusions.

The original round and its source archive remain immutable historical evidence.
No native fits, exports, consumer replay or unaffected transfers are repeated.
"""
import argparse
import copy
import fcntl
import json
from pathlib import Path
import shutil

import numpy as np
from threadpoolctl import threadpool_limits

from ..studies import mapped_vibe_round as engine
from ..support.external_experiment import LAB, digest, save, stamp
from .mapped_vibe_metrics import summary
from .transfer_identity import exclusion
from ..studies.vibe_specialist_round import (EXPERIMENTS, input_arrays, inside, load,
                                           receipt_valid, stage, versions)

BASE = engine.DEFAULT
BASE_SHA256 = '5d8744ca36ce0037d47f756333e26ba5f04198c0faa1a4b56c9f6e51d49a32dc'
DEFAULT = LAB / 'local/mapped-vibes-20261029/component-repair-1'
EXPECTED = {('pmemo', 'amg1608-matched'), ('pmemo', 'ntwicm-matched')}


def sources():
    paths = [Path(__file__), Path(__file__).with_name('test_transfer_identity.py')]
    return {**engine.sources(), **{str(p.relative_to(EXPERIMENTS)): digest(p) for p in paths}}


def base_files():
    plan = load(BASE / 'plan.json', BASE_SHA256)
    files = {'plan.json': BASE_SHA256, 'summary.json': digest(BASE / 'summary.json')}
    for relative, expected in plan['inputs'].items():
        if digest(inside(BASE / relative)) != expected:
            raise ValueError('Original frozen input changed: ' + relative)
        files[relative] = expected
    for relative, expected in plan['code'].items():
        archived = 'source/' + relative
        if digest(inside(BASE / archived)) != expected:
            raise ValueError('Original archived source changed: ' + relative)
        files[archived] = expected
        # This extension intentionally updates only the exclusion policy and
        # entrypoint. Other numerical dependencies must match the old round.
        if relative not in ('audio_lab_tools/mapped_vibe_round.py', 'run.py'):
            if digest(inside(EXPERIMENTS / relative)) != expected:
                raise ValueError('Unrelated dependency changed: ' + relative)
    receipts = sorted(BASE.glob('*.receipt.json'))
    if len(receipts) != 47:
        raise ValueError('Expected the original 47 completed stage receipts')
    for path in receipts:
        name = path.name.removesuffix('.receipt.json')
        if not receipt_valid(BASE, name, BASE_SHA256):
            raise ValueError('Original stage incomplete: ' + name)
        files[path.name] = digest(path)
        files.update(load(path)['outputs'])
    return plan, files


def freeze(directory):
    if (directory / 'plan.json').exists():
        return validate(directory)
    if directory.exists() and list(directory.iterdir()):
        raise ValueError('Preserve the nonempty directory and choose another --directory')
    original, files = base_files()
    audit, affected = {}, []
    for source, target in original['transfer']:
        key = source + '__' + target
        source_meta = load(BASE / f'inputs/{source}.json')
        target_meta = load(BASE / f'inputs/{target}.json')
        _, item = exclusion(source_meta['rows'], target_meta['rows'])
        saved = load(BASE / f'transfer-{key}.report.json')
        if sorted(saved['excluded_ids']) != sorted(item['direct_excluded_ids']):
            raise ValueError('Direct exclusions do not reproduce original report: ' + key)
        item['original_training_rows'] = saved['training_rows']
        item['original_state'] = saved['state']
        audit[key] = item
        if item['additional_excluded_ids']:
            affected.append((source, target))
    if set(affected) != EXPECTED:
        raise ValueError('Unexpected affected directions; inspect metadata before fitting: ' + repr(affected))
    directory.mkdir(parents=True, exist_ok=True)
    names = sorted({name for pair in affected for name in pair})
    for relative in ['inputs/rules.json', *[f'inputs/{name}.{suffix}' for name in names for suffix in ('json', 'npz')]]:
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BASE / relative, destination)
        if digest(destination) != original['inputs'][relative]:
            raise ValueError('Input copy changed: ' + relative)
    save(directory / 'inputs/identity-audit.json', audit)
    code = sources()
    for relative, expected in code.items():
        destination = directory / 'source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXPERIMENTS / relative, destination)
        if digest(destination) != expected:
            raise ValueError('Code changed during freeze: ' + relative)
    plan = {'format': 'connected-source-transfer-repair-v1', 'created_at': stamp(),
            'base_directory': str(BASE), 'base_plan_sha256': BASE_SHA256,
            'base_files': files, 'code': code, 'versions': versions(),
            'inputs': {str(p.relative_to(directory)): digest(p) for p in sorted((directory / 'inputs').iterdir())},
            'transfer': affected, 'grid': original['grid'],
            'policy': 'Exclude source artist/recording/audio connected components anchored to any supplied target identity; title-only matches exclude rows without expanding components; missing identities and coarse groups are not anchors.',
            'selection': original['selection'], 'primary': original['primary'],
            'ensemble': original['transfer_ensemble'],
            'unchanged': ['native fits', 'fixed mappings', 'six-scorer export', 'consumer replay', '30 unaffected transfer receipts'],
            'local_labels_used': False, 'target_labels_used_for_selection': False,
            'limits': original['limits']}
    save(directory / 'plan.json', plan)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
          'affected': {key: {'old': item['original_training_rows'], 'new': item['training_rows'],
                            'additional_excluded': len(item['additional_excluded_ids'])}
                       for key, item in audit.items() if item['additional_excluded_ids']},
          'unchanged_transfer_directions': 30}), flush=True)
    return plan


def validate(directory, *, execution=True):
    plan = load(directory / 'plan.json')
    if plan['base_plan_sha256'] != BASE_SHA256 or plan['base_directory'] != str(BASE):
        raise ValueError('Unexpected original round')
    if execution and (plan['code'] != sources() or plan['versions'] != versions()):
        raise ValueError('Repair code/runtime changed; preserve it and use a new directory')
    for relative, expected in plan['base_files'].items():
        if digest(inside(BASE / relative)) != expected:
            raise ValueError('Original evidence changed: ' + relative)
    for prefix, entries in (('', plan['inputs']), ('source/', plan['code'])):
        for relative, expected in entries.items():
            path = inside(directory / (prefix + relative))
            if not path.is_relative_to(directory) or digest(path) != expected:
                raise ValueError('Repair input/archive changed: ' + relative)
    return plan


def repair_stage(directory, source, target):
    paths = engine.transfer_stage(directory, source, target)
    report = load(paths[0])
    audit = load(directory / 'inputs/identity-audit.json')[source + '__' + target]
    if report['excluded_ids'] != audit['excluded_ids'] or report['training_rows'] != audit['training_rows']:
        raise ValueError('Fit exclusions differ from frozen metadata audit')
    report['identity_component_audit'] = audit
    report['supersedes'] = {'base_plan_sha256': BASE_SHA256,
                           'report_sha256': digest(BASE / paths[0].name),
                           'receipt_sha256': digest(BASE / f'transfer-{source}__{target}.receipt.json')}
    save(paths[0], report)
    return paths


def report_stage(directory):
    plan = load(directory / 'plan.json')
    result = copy.deepcopy(load(BASE / 'summary.json'))
    fingerprint = digest(directory / 'plan.json')
    replacements = {tuple(pair) for pair in plan['transfer']}
    lineage = {}
    for source, target in plan['transfer']:
        name = f'transfer-{source}__{target}'
        if not receipt_valid(directory, name, fingerprint):
            raise ValueError('Finish both repaired transfers before summarizing')
        report = load(directory / f'{name}.report.json')
        result['transfers'][source + '__' + target] = {k: report[k] for k in ('state', 'training_rows', 'excluded_ids', 'vibes')}
    for source, target in engine.TRANSFER:
        name = f'transfer-{source}__{target}'
        origin = directory if (source, target) in replacements else BASE
        lineage[source + '__' + target] = {'directory': str(origin),
            'receipt_sha256': digest(origin / f'{name}.receipt.json'),
            'report_sha256': digest(origin / f'{name}.report.json'),
            'predictions_sha256': digest(origin / f'{name}.oof.npz')}
    for target in sorted({b for _, b in replacements}):
        arrays, contributors = [], []
        for source, other in engine.TRANSFER:
            key = source + '__' + target
            if other != target or result['transfers'][key]['state'] != 'complete':
                continue
            origin = directory if (source, target) in replacements else BASE
            arrays.append(input_arrays(origin / f'transfer-{key}.oof.npz'))
            contributors.append(source)
        common = set.intersection(*(set(a) for a in arrays))
        metrics = {}
        for key in sorted(common):
            if not key.endswith('__selected'):
                continue
            observed = key.replace('__selected', '__observed')
            if any(not np.array_equal(a['outer'], arrays[0]['outer']) or
                   not np.array_equal(a[observed], arrays[0][observed]) for a in arrays):
                raise ValueError('Ensemble target row/reference alignment changed')
            metrics[key.split('__')[0]] = summary(arrays[0][observed],
                np.mean([a[key] for a in arrays], axis=0), arrays[0]['outer'])
        result['ensembles'][target] = metrics
        result.setdefault('repaired_ensemble_sources', {})[target] = contributors
    counts = {}
    for vibe in engine.VIBES:
        tasks = [r['vibes'][vibe] for r in result['transfers'].values() if vibe in r['vibes']]
        if tasks:
            counts[vibe] = {'evaluated': len(tasks),
                           'positive_ndcg_gain': sum(t['selected']['ndcg_gain'] > 0 for t in tasks),
                           'mse_below_source_mean': sum(t['selected']['mse'] < t['training_mean']['mse'] for t in tasks)}
    result.update(plan_sha256=fingerprint, base_plan_sha256=BASE_SHA256,
                  transfer_lineage=lineage, transfer_counts=counts,
                  superseded_transfer_directions=[a + '__' + b for a, b in plan['transfer']],
                  original_evidence_unchanged=True, state='complete')
    save(directory / 'summary.json', result)
    print(json.dumps({'state': 'complete', 'transfer_counts': counts,
          'summary': str(directory / 'summary.json')}), flush=True)
    return [directory / 'summary.json']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--independent-execution', action='store_true',
                        help='Compatibility option; research always runs independently of pytest')
    args = parser.parse_args()
    directory = inside(args.directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with (directory.parent / (directory.name + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit('This transfer repair is already running') from error
        if args.action == 'freeze':
            freeze(directory)
            return
        # Historical verification checks pinned archives, inputs and receipts.
        # Only fitting requires today's executable code/runtime to match too.
        plan = validate(directory, execution=args.action != 'verify')
        if args.action == 'verify':
            names = [f'transfer-{a}__{b}' for a, b in plan['transfer']] + ['summary']
            if not all(receipt_valid(directory, name, digest(directory / 'plan.json')) for name in names):
                raise ValueError('Repair outputs incomplete')
            print(json.dumps({'verified': names, 'original_files_unchanged': len(plan['base_files'])}), flush=True)
            return
        with threadpool_limits(limits=1):
            for source, target in plan['transfer']:
                stage(directory, f'transfer-{source}__{target}',
                      lambda s=source, t=target: repair_stage(directory, s, t))
            stage(directory, 'summary', lambda: report_stage(directory))
        validate(directory)


if __name__ == '__main__':
    main()
