"""All-source extension of the frozen first per-vibe batch; no grid retuning."""
from pathlib import Path
import shutil

import numpy as np

from . import vibe_specialist_round as engine
from .external_experiment import LAB, digest, save

PRIOR = LAB / 'local/per-vibe-20261029/round-1'
DEFAULT = LAB / 'local/per-vibe-20261029/all-sources-1'
FIRST = engine.NATIVE
BASE_SOURCES = engine.sources
BASE_FREEZE = engine.freeze


def sources():
    files = BASE_SOURCES()
    for path in (Path(__file__), LAB / 'PER_VIBE_ALL_SOURCES.md',
                 Path(__file__).with_name('test_vibe_specialist_all.py')):
        files[str(path.relative_to(engine.EXPERIMENTS))] = digest(path)
    return files


def verify_reuse(prior, destination, dataset):
    previous, current = engine.load(prior / 'plan.json'), engine.load(destination / 'plan.json')
    if previous['native_grid'] != current['native_grid'] or previous['seed'] != current['seed']:
        raise ValueError('Native reuse grid or seed changed')
    if previous['versions'] != current['versions']:
        raise ValueError('Native reuse numerical runtime changed')
    # The entrypoint gained this new dispatcher. All fitting, selection and
    # inference source bytes must still match the first batch.
    for relative, expected in previous['code'].items():
        if relative != 'run.py' and current['code'].get(relative) != expected:
            raise ValueError('Native reuse fitting/protocol code changed: ' + relative)
    for extension in ('json', 'npz'):
        relative = f'inputs/{dataset}.{extension}'
        if extension == 'json':
            if engine.load(prior / relative) != engine.load(destination / relative):
                raise ValueError('Native reuse metadata changed: ' + dataset)
        else:
            old, new = engine.input_arrays(prior / relative), engine.input_arrays(destination / relative)
            if old.keys() != new.keys() or any(not np.array_equal(old[k], new[k]) for k in old):
                raise ValueError('Native reuse arrays changed: ' + dataset)
    name = 'native-' + dataset
    fingerprint = digest(prior / 'plan.json')
    if not engine.receipt_valid(prior, name, fingerprint):
        raise ValueError('Native reuse requires a completed verified receipt: ' + name)
    return engine.load(prior / f'{name}.receipt.json')


def freeze(directory):
    if (directory / 'plan.json').exists():
        return engine.validate(directory)
    BASE_FREEZE(directory)
    reuse = {dataset: verify_reuse(PRIOR, directory, dataset) for dataset in FIRST}
    plan = engine.load(directory / 'plan.json')
    plan['extension'] = 'all thirteen native sources; same model grids and selectors'
    plan['prior_native_reuse'] = {'directory': str(PRIOR), 'plan_sha256': digest(PRIOR / 'plan.json'),
                                 'receipts': {dataset: digest(PRIOR / f'native-{dataset}.receipt.json')
                                              for dataset in reuse},
                                 'checks': 'identical arrays, metadata, grid, seed, runtime and fitting/protocol code; output hashes verified'}
    save(directory / 'plan.json', plan)
    fingerprint = digest(directory / 'plan.json')
    for dataset, receipt in reuse.items():
        for relative in receipt['outputs']:
            shutil.copyfile(PRIOR / relative, directory / relative)
        save(directory / f'native-{dataset}.receipt.json', {
            **receipt, 'fingerprint': fingerprint, 'reused_from': str(PRIOR),
            'original_receipt_sha256': plan['prior_native_reuse']['receipts'][dataset]})
        engine.receipt_valid(directory, 'native-' + dataset, fingerprint)
    return plan


def report(directory):
    plan = engine.load(directory / 'plan.json')
    fingerprint = digest(directory / 'plan.json')
    summary = {'plan_sha256': fingerprint, 'native': {}, 'local': None,
               'local_status': plan['local_status'], 'product_adoption': False,
               'prior_native_reuse': plan['prior_native_reuse']}
    for dataset in engine.NATIVE:
        if engine.receipt_valid(directory, 'native-' + dataset, fingerprint):
            native = engine.load(directory / f'native-{dataset}.report.json')
            summary['native'][dataset] = {k: native[k] for k in ('rows', 'groups', 'metrics', 'paired_fixed_oof')}
    if engine.receipt_valid(directory, 'local', fingerprint):
        local = engine.load(directory / 'local.report.json')
        summary['local'] = {k: local[k] for k in ('metrics', 'paired_fixed_oof', 'rows', 'groups', 'limitations',
            'candidate_count', 'model_bytes', 'complete_research_bundle_bytes', 'inference',
            'independent_export_max_absolute_error', 'independent_bundle_max_absolute_error')}
    summary['state'] = 'complete' if len(summary['native']) == len(engine.NATIVE) and summary['local'] else 'partial'
    save(directory / 'summary.json', summary)
    # Detailed figures stay in summary.json; avoid printing thousands of lines.
    print({'state': summary['state'], 'native_sources': len(summary['native']),
           'local_complete': bool(summary['local']), 'summary': str(directory / 'summary.json')}, flush=True)
    return summary


def main():
    heads = engine.load(PRIOR / 'inputs/frozen-heads.json')
    engine.NATIVE = (*FIRST, *[source for source in heads if source not in FIRST])
    engine.DEFAULT = DEFAULT
    engine.sources, engine.freeze, engine.report = sources, freeze, report
    engine.main()


if __name__ == '__main__':
    main()
