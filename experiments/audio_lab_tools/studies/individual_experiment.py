"""Pinned source-native research for #577–#580; never installs a scorer."""
import argparse
import json
from pathlib import Path
import shutil
import sys

import numpy as np
from threadpoolctl import threadpool_limits

from . import individual_protocol as protocol
from ..support.external_experiment import LAB, digest, save, stamp
from ..training.external_training import folds
from ..focus.focus_experiment import (arrays, check_receipt, group_guard, head_predictions,
                                    inside, load, runtime)
from .mapped_vibe_rules import registry

BASE = LAB / 'local/mapped-vibes-20261029'
NATIVE = BASE / 'round-1'
COMMON = BASE / 'common-target-1'
BASELINES = BASE / 'cruise-baselines-1'
BROWSER = LAB / 'local/preprocessing-quality-20261029/run-1'
LOCAL = LAB / 'local/individual-20261030/local-1'
DEFAULT = LAB / 'local/individual-20261030/remaining-1'
EXPERIMENTS = LAB.parent
SOURCES = ('emotify-artist', 'jamendo-human-4shards', 'soundtracks-all',
           'coimbra-mirex-like', 'emma-preview-matched', 'pmemo', 'deam', 'cal500-raw-activity')
PILOTS = ('manymusic-natural', 'manymusic-generated', 'memo2496-pilot')
PRIMARY = (*PILOTS, 'soundtracks-all', 'cal500-raw-activity')
VIBES = ('release', 'energize', 'relax', 'uplift')
TRANSFER = (('pmemo', 'deam'), ('deam', 'pmemo'),
            ('emotify-artist', 'jamendo-human-4shards'), ('jamendo-human-4shards', 'emotify-artist'),
            ('soundtracks-all', 'emotify-artist'), ('soundtracks-all', 'jamendo-human-4shards'))


def compact(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(',', ':'), allow_nan=False) + '\n')


def receipt(directory, name, outputs):
    save(directory / (name + '.receipt.json'), {'plan_sha256': digest(directory / 'plan.json'),
         'created_at': stamp(), 'outputs': {str(p.relative_to(directory)): digest(p) for p in outputs}})


def code_paths():
    # Import the report before pinning the actual module closure.
    from . import individual_evaluation  # noqa: F401
    paths = {Path(module.__file__).resolve() for name, module in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(module, '__file__', None)}
    paths.update((Path(__file__).resolve(), EXPERIMENTS / 'run.py',
                  LAB / 'INDIVIDUAL_EXPERIMENT_PLAN.md',
                  Path(__file__).with_name('test_individual_experiment.py'),
                  Path(__file__).with_name('test_individual_results.py')))
    return {str(path.relative_to(EXPERIMENTS)): path for path in sorted(paths)}


def freeze(directory):
    if directory.exists():
        raise ValueError('Preserve previous evidence; choose a new directory')
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    plans = {base: load(ref(base / 'plan.json')) for base in (NATIVE, COMMON, BASELINES, BROWSER)}
    for base, name in ((BASELINES, 'evaluation.receipt.json'), (BROWSER, 'evaluation.receipt.json'),
                       (COMMON, 'summary.receipt.json')):
        check_receipt(base, name)
        ref(base / name)
    check_receipt(LOCAL, 'receipt.json', False)
    ref(LOCAL / 'receipt.json')
    identities = load(ref(LOCAL / 'identities.json'))
    metadata, data, historic = {}, {}, {}
    for name in (*SOURCES, *PILOTS):
        base = COMMON if name in PILOTS else NATIVE
        for suffix in ('json', 'npz'):
            path = ref(base / f'inputs/{name}.{suffix}')
            if digest(path) != plans[base]['inputs'][f'inputs/{name}.{suffix}']:
                raise ValueError('Prior input changed: ' + str(path))
        metadata[name] = load(base / f'inputs/{name}.json')
        data[name] = arrays(base / f'inputs/{name}.npz')
        groups = protocol.components(metadata[name]['rows'], include_declared=True)
        group_guard(groups, data[name]['outer'])
        metadata[name]['groups'] = groups.tolist()
        if name in SOURCES:
            check_receipt(NATIVE, f'dataset-{name}.receipt.json')
            ref(NATIVE / f'dataset-{name}.receipt.json')
            old = arrays(ref(NATIVE / (name + '.oof.npz')))
            prior_path = ref(NATIVE / f'inputs/{name}.prior-oof.npz')
            if digest(prior_path) != plans[NATIVE]['inputs'][f'inputs/{name}.prior-oof.npz']:
                raise ValueError('Prior native predictions changed')
            historic[name] = {'prior_native': arrays(prior_path)['independent'],
                             **{key: value for key, value in old.items() if key.endswith(('__selected', '__native_mse'))}}
    directory.mkdir(parents=True)
    (directory / 'inputs').mkdir()
    exclusions = {}
    for name in SOURCES:
        rows, value = metadata[name]['rows'], data[name]
        native_keep, native_audit = protocol.exclude(rows, identities)
        protected = [*identities, *[row for other, meta in metadata.items() if other != name for row in meta['rows']]]
        transfer_keep, transfer_audit = protocol.exclude(rows, protected)
        groups = np.asarray(metadata[name]['groups'])
        if native_keep.sum() < 30 or transfer_keep.sum() < 30 or len(set(groups[transfer_keep])) < 3:
            raise ValueError('Insufficient protected source: ' + name)
        inner = np.full((len(rows), 6), -1, dtype=int)
        inner[native_keep] = protocol.split_inner(groups[native_keep], value['outer'][native_keep])
        transfer_inner = np.full(len(rows), -1, dtype=int)
        transfer_inner[transfer_keep] = folds(groups[transfer_keep], count=3, seed=protocol.SEED)
        group_guard(groups[transfer_keep], transfer_inner[transfer_keep])
        value.update(native_keep=native_keep, transfer_keep=transfer_keep, native_inner=inner, transfer_inner=transfer_inner)
        exclusions[name] = {'native': native_audit, 'transfer': transfer_audit}
        np.savez_compressed(directory / f'inputs/{name}.historic.npz', **historic[name])
    baseline = arrays(ref(BASELINES / 'external-predictions.npz'))
    browser_scores = arrays(ref(BROWSER / 'predictions.npz'))
    # The named prior full-source heads must also be disjoint from primary rows.
    prior_sources = [*metadata['pmemo']['rows'], *metadata['emotify-artist']['rows'],
                     *metadata['jamendo-human-4shards']['rows']]
    for name in PRIMARY:
        rows = metadata[name]['rows']
        prior_keep, audit = protocol.exclude(rows, prior_sources)
        original = baseline[name + '__retained']
        if not np.array_equal(original, browser_scores[name + '__retained']):
            raise ValueError('Shared/browser row masks differ')
        data[name]['common'] = original & prior_keep
        metadata[name]['common_exclusions'] = {'original_shared_disjoint_rows': int(original.sum()),
                                              'common_rows': int(data[name]['common'].sum()), 'named_priors': audit}
        browser_meta = load(ref(BROWSER / f'inputs/{name}.json'))
        if browser_meta['rows'] != rows:
            raise ValueError('Browser target row identity differs')
        chunks, offsets = [], [0]
        cases = [case for case in plans[BROWSER]['cases'] if case['cohort'] == name]
        if len(cases) != len(rows):
            raise ValueError('Browser target coverage differs')
        for index, case in enumerate(cases):
            if case['index'] != index or case['id'] != rows[index]['id']:
                raise ValueError('Browser target order differs')
            parent = BROWSER / f'cases/{name}/{index:04d}'
            evidence = load(ref(parent / 'receipt.json'))
            path = ref(parent / 'report.json')
            if evidence['plan_sha256'] != digest(BROWSER / 'plan.json') or evidence['outputs']['report.json'] != digest(path):
                raise ValueError('Browser case receipt differs')
            result = load(path)
            if result['case'] != case or [chunk['start'] for chunk in result['chunks']] != case['starts']:
                raise ValueError('Browser excerpt alignment differs')
            chunks.extend(chunk['browser']['embedding'] for chunk in result['chunks'])
            offsets.append(len(chunks))
        data[name]['browser'] = np.asarray(chunks)
        if name not in PILOTS:
            data[name]['offsets'] = np.arange(len(rows) + 1)
        if not np.array_equal(data[name]['offsets'], offsets) or data[name]['browser'].shape != data[name]['embedding'].shape:
            raise ValueError('Preparation chunk coverage differs')
    for name in metadata:
        save(directory / f'inputs/{name}.json', metadata[name])
        np.savez_compressed(directory / f'inputs/{name}.npz', **data[name])
    np.savez_compressed(directory / 'inputs/shared.npz', **baseline)
    np.savez_compressed(directory / 'inputs/browser_shared.npz', **browser_scores)
    save(directory / 'inputs/exclusions.json', exclusions)
    rules = registry()
    for name in PILOTS:
        rules[name] = rules['pmemo']
    save(directory / 'inputs/rules.json', rules)
    # Exact named exported models, kept as controls rather than retuned.
    priors = {}
    for source, vibe in (('emotify-artist', 'relax'), ('pmemo', 'uplift'),
                         ('pmemo', 'energize'), ('jamendo-human-4shards', 'release')):
        path = ref(NATIVE / (source + '.experts.json'))
        priors[vibe] = load(path)['experts'][vibe]
    compact(directory / 'inputs/named-priors.json', priors)
    check_receipt(NATIVE, 'dataset-merge-quadrants.receipt.json')
    ref(NATIVE / 'dataset-merge-quadrants.receipt.json')
    shutil.copyfile(ref(NATIVE / 'merge-quadrants.report.json'), directory / 'inputs/retained-merge-proxy.report.json')
    # Preserve source-selected older comparisons, including prior failures.
    for regime, sources in (('pilots', ('pmemo', 'deam', 'emotify-artist')),
                            ('soundtracks', ('pmemo', 'deam', 'emotify-artist', 'jamendo-human-4shards', 'coimbra-mirex-like'))):
        for source in sources:
            check_receipt(COMMON, f'{regime}-{source}.receipt.json')
            ref(COMMON / f'{regime}-{source}.receipt.json')
            origin = ref(COMMON / f'{regime}-{source}.predictions.npz')
            shutil.copyfile(origin, directory / 'inputs' / ('prior-' + origin.name))
    for name in ('checkpoint', 'refresh_calibration', 'refresh_both'):
        shutil.copyfile(ref(BASELINES / (name + '.json')), directory / 'inputs' / (name + '.json'))
    code = code_paths()
    for relative, source in code.items():
        destination = directory / 'source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    plan = {'created_at': stamp(), 'tickets': [577, 578, 579, 580], 'runtime': runtime(),
            'sources': SOURCES, 'primary': PRIMARY, 'reciprocal': TRANSFER, 'seed': protocol.SEED,
            'grid': [{'family': 'mean'}, *protocol.SPECS], 'representations': protocol.FAMILIES,
            'selection': 'independent native MSE; fixed representation; source-only grouped folds',
            'aggregation': 'mean native chunk predictions then fixed mapping', 'bootstrap_draws': 1000,
            'embedding_signature': plans[NATIVE]['embedding_signature'], 'attribute_names': plans[NATIVE]['attribute_names'],
            'local': str(LOCAL), 'references': references,
            'code': {name: digest(path) for name, path in code.items()},
            'inputs': {str(path.relative_to(directory)): digest(path) for path in (directory / 'inputs').iterdir()},
            'budgets': {'single_scorer_bytes': 262144, 'ensemble_bytes': 524288,
                        'warm_three_excerpt_ms': 100, 'process_rss_bytes': 1073741824},
            'jamendo_new_artifacts': {'status': 'research only; no inherited product-terms acceptance',
               'terms_source': 'https://github.com/MTG/mtg-jamendo-dataset#license',
               'publisher_commit': 'cafd8e20c265ed84f1e61f1c875327971f43a62f',
               'checked_date': '2026-10-30', 'metadata': 'CC BY-NC-SA 4.0',
               'audio': 'individual Creative Commons licenses; publisher limits dataset to noncommercial research/academic use'},
            'limits': ['Previously inspected cohorts; no pristine confirmation.',
                       'Native constructs and annotation scales differ; compare methods within each task.',
                       'Missing names/aliases and unknown backbone training overlap remain unresolved.',
                       'Pilot/browser data lack aligned historical acoustic attributes.',
                       'Python resource measurements exclude the encoder and application startup.',
                       'Local positive-only labels do not supply negative or causal listening outcomes.']}
    save(directory / 'plan.json', plan)
    validate(directory, execution=True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
          'sources': {name: {'native': int(data[name]['native_keep'].sum()), 'transfer': int(data[name]['transfer_keep'].sum())} for name in SOURCES},
          'primary_rows': {name: int(data[name]['common'].sum()) for name in PRIMARY}}), flush=True)


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    if execution and plan['runtime'] != runtime():
        raise ValueError('Numerical runtime changed')
    for relative, expected in plan['inputs'].items():
        if digest(directory / relative) != expected:
            raise ValueError('Frozen input changed: ' + relative)
    for relative, expected in plan['code'].items():
        if digest(directory / 'source' / relative) != expected:
            raise ValueError('Archived code changed: ' + relative)
        if execution and digest(EXPERIMENTS / relative) != expected:
            raise ValueError('Execution code changed; preserve round: ' + relative)
    for path, expected in plan['references'].items():
        if digest(path) != expected:
            raise ValueError('Referenced evidence changed: ' + path)
    return plan


def fit_source(directory, source, plan):
    stage = 'fit-' + source
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    data = arrays(directory / f'inputs/{source}.npz')
    meta = load(directory / f'inputs/{source}.json')
    keep = data['native_keep']
    y, outer, inner = data['y'][keep], data['outer'][keep], data['native_inner'][keep]
    predictions, traces, artifacts, errors, outputs = {}, {}, {}, {}, []
    for family in protocol.FAMILIES:
        all_x = protocol.features(data, family)
        x = all_x[keep]
        predicted = np.empty_like(y)
        traces[family] = {}
        for fold in range(5):
            train, valid = outer != fold, outer == fold
            heads, trace = protocol.select(x[train], y[train], inner[train, fold])
            predicted[valid] = head_predictions(heads, x[valid])
            traces[family][str(fold)] = trace
        predictions[family] = predicted
        train = data['transfer_keep']
        heads, trace = protocol.select(all_x[train], data['y'][train], data['transfer_inner'][train])
        traces[family]['protected_transfer'] = trace
        artifact = {'format': 'native-conditional-mean-v1', 'source': source, 'family': family,
                    'target_names': meta['target_names'], 'heads': heads,
                    'training_rows': int(train.sum()), 'plan_sha256': digest(directory / 'plan.json'),
                    'embedding_signature': plan['embedding_signature'],
                    'attribute_names': plan['attribute_names'] if family != 'embedding' else [],
                    'source_mean': data['y'][train].mean(axis=0).tolist(),
                    'source_median': np.median(data['y'][train], axis=0).tolist()}
        path = directory / f'models/{source}.{family}.json'
        compact(path, artifact)
        loaded = load(path)
        difference = np.max(np.abs(head_predictions(loaded['heads'], all_x) - head_predictions(loaded['heads'], all_x, True)), axis=0)
        if np.any(difference > 1e-10):
            raise ValueError('Independent per-native-head replay differs')
        artifacts[family], errors[family] = str(path.relative_to(directory)), difference.tolist()
        outputs.append(path)
    for name, operation in (('training_mean', np.mean), ('training_median', np.median)):
        predictions[name] = np.empty_like(y)
        for fold in range(5):
            predictions[name][outer == fold] = operation(y[outer != fold], axis=0)
    output = directory / (source + '.native-oof.npz')
    np.savez_compressed(output, **predictions)
    outputs.append(output)
    report = directory / (source + '.fit.json')
    save(report, {'selection': traces, 'per_head_replay_error': errors, 'artifacts': artifacts,
                  'native_rows': len(y), 'transfer_rows': int(data['transfer_keep'].sum())})
    outputs.append(report)
    validate(directory, execution=True)
    receipt(directory, stage, outputs)
    print(json.dumps({'fitted': source, 'native_rows': len(y), 'transfer_rows': int(data['transfer_keep'].sum()),
                      'max_replay_error': max(max(error) for error in errors.values())}), flush=True)


def main():
    from . import individual_evaluation as evaluation
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'fit', 'evaluate', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--source', choices=SOURCES)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
        elif args.action == 'fit':
            plan = validate(directory, execution=True)
            for source in (args.source,) if args.source else SOURCES:
                fit_source(directory, source, plan)
        elif args.action == 'evaluate':
            evaluation.run(directory)
        else:
            validate(directory)
            for source in SOURCES:
                check_receipt(directory, 'fit-' + source + '.receipt.json')
            check_receipt(directory, 'evaluation.receipt.json')
            print(json.dumps({'verified': str(directory)}))


if __name__ == '__main__':
    main()
