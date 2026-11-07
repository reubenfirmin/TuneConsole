"""Common-target external candidate comparisons, without local outcome reads."""
import argparse
import fcntl
import json
from pathlib import Path
import shutil

import numpy as np
from threadpoolctl import threadpool_limits

from . import mapped_vibe_round as engine
from . import mapped_vibe_inference as inference
from .. import source_path
from ..datasets import dataset_windows as windows
from ..support.external_experiment import LAB, digest, save, stamp
from ..training.external_training import folds, validation_groups
from ..evaluation.mapped_vibe_metrics import summary
from .mapped_vibe_rules import apply
from ..evaluation.transfer_identity import exclusion
from .vibe_specialist_round import (EXPERIMENTS, input_arrays, inside, load, npz,
                                    receipt_valid, stage, versions)

DEFAULT = LAB / 'local/mapped-vibes-20261029/common-target-1'
PILOTS = ('manymusic-natural', 'manymusic-generated', 'memo2496-pilot')
SOURCE_SETS = {'pilots': ('pmemo', 'deam', 'emotify-artist'),
               'soundtracks': ('pmemo', 'deam', 'emotify-artist', 'jamendo-human-4shards', 'coimbra-mirex-like')}
TARGET_SETS = {'pilots': PILOTS, 'soundtracks': ('soundtracks-all',)}
SEED = 53031


def codes():
    paths = [Path(__file__), source_path('dataset_windows.py'),
             Path(__file__).with_name('test_candidate_followup.py'),
             LAB / 'CANDIDATE_FOLLOWUP_PLAN.md']
    return {**engine.sources(), **{str(p.relative_to(EXPERIMENTS)): digest(p) for p in paths}}


def average_chunks(values, offsets):
    return np.asarray([values[a:b].mean(axis=0) for a, b in zip(offsets[:-1], offsets[1:])])


def freeze(directory):
    if (directory / 'plan.json').exists():
        return validate(directory)
    if directory.exists() and list(directory.iterdir()):
        raise ValueError('Preserve the nonempty directory and choose another --directory')
    original = load(engine.DEFAULT / 'plan.json', '5d8744ca36ce0037d47f756333e26ba5f04198c0faa1a4b56c9f6e51d49a32dc')
    directory.mkdir(parents=True, exist_ok=True)
    references = {}

    def reference(path, expected=None):
        path = inside(path)
        value = load(path, expected)
        references[str(path)] = digest(path)
        return value

    def original_copy(relative):
        source = engine.DEFAULT / relative
        expected = original['inputs'].get(relative)
        if expected is None:
            raise ValueError('Input absent from original manifest')
        if digest(source) != expected:
            raise ValueError('Original input changed: ' + relative)
        references[str(source)] = expected
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    for name in sorted({s for names in SOURCE_SETS.values() for s in names} | {'soundtracks-all', 'cal500-raw-activity'}):
        for suffix in ('json', 'npz'):
            original_copy(f'inputs/{name}.{suffix}')
    original_copy('inputs/rules.json')
    rules = load(directory / 'inputs/rules.json')
    # Read the released pilot plan by its stored hash. Its archived extraction
    # provenance, not today's mutable application sources, defines these caches.
    index = reference(windows.WORK / 'current-plan.json')
    pilot_plan = reference(index['path'], index['sha256'])
    extracted = reference(windows.WORK / 'features.json')
    if extracted['state'] != 'complete' or extracted['provenance']['plan_sha256'] != index['sha256']:
        raise ValueError('Pilot extraction is not complete for its declared plan')
    planned = {row['id']: row for row in pilot_plan['rows']}
    accounted = [row['id'] for row in extracted['rows'] + extracted['errors']]
    if len(accounted) != len(set(accounted)) or set(accounted) != set(planned):
        raise ValueError('Unaccounted pilot rows/errors')
    signatures = set()
    for cohort in PILOTS:
        records = [row for row in extracted['rows'] if row['subset'] == cohort]
        chunks, offsets = [], [0]
        for row in records:
            if any(row.get(k) != v for k, v in planned[row['id']].items()):
                raise ValueError('Pilot row changed from declared plan')
            windows.check_interval(row, row['duration'])
            if [c['start'] for c in row['chunks']] != row['chunk_starts']:
                raise ValueError('Pilot chunk sequence changed')
            for chunk in row['chunks']:
                path = inside(chunk['path'])
                if chunk['seconds'] != 10 or digest(path) != chunk['sha256']:
                    raise ValueError('Pilot chunk checksum/duration changed')
                checked = windows.checked_record(path, {'audio_sha256': row['audio_sha256'],
                    'start': chunk['start'], 'seconds': 10, 'provenance': extracted['provenance']})
                references[str(path)] = chunk['sha256']
                if 'reused_from' in checked:
                    origin = checked['reused_from']
                    references[str(inside(origin['path']))] = origin['sha256']
                chunks.append(windows.feature_vectors(checked['result']))
                signatures.add(windows.embedding_signature(checked['result']))
            offsets.append(len(chunks))
        groups = validation_groups(records)
        if len(records) < 10 or len(set(groups)) < 5:
            raise ValueError('Insufficient pilot groups: ' + cohort)
        y = np.array([row['targets'] for row in records], dtype=float)
        if any(row['target_names'] != ['valence', 'arousal'] for row in records) or not np.isfinite(y).all() or (y < 0).any() or (y > 1).any():
            raise ValueError('Invalid normalized pilot annotations')
        meta = {'dataset': cohort, 'rows': records, 'groups': groups, 'target_names': ['valence', 'arousal'],
                'annotation_unit': 'one recording, complete released rated interval', 'missing_acoustic_attributes': True}
        save(directory / f'inputs/{cohort}.json', meta)
        offsets = np.asarray(offsets)
        npz(directory / f'inputs/{cohort}.npz', embedding=np.asarray(chunks), offsets=offsets,
            y=y, outer=folds(groups, seed=SEED))
        rules[cohort] = rules['pmemo']
    if signatures != {original['embedding_signature']}:
        raise ValueError('Pilot and source embedding signatures differ')
    save(directory / 'inputs/rules.json', rules)
    for relative in ('cal500-raw-activity.oof.npz', 'cal500-raw-activity.experts.json'):
        name = 'dataset-cal500-raw-activity'
        receipt = reference(engine.DEFAULT / f'{name}.receipt.json')
        if not receipt_valid(engine.DEFAULT, name, digest(engine.DEFAULT / 'plan.json')):
            raise ValueError('Focus source receipt not verified')
        source = engine.DEFAULT / relative
        if digest(source) != receipt['outputs'][relative]:
            raise ValueError('Focus artifact changed')
        references[str(source)] = digest(source)
        shutil.copyfile(source, directory / 'inputs' / relative)
    prior_path = engine.PRIOR / 'native-cal500-raw-activity.model.json'
    prior_receipt = reference(engine.PRIOR / 'native-cal500-raw-activity.receipt.json')
    prior = reference(prior_path, prior_receipt['outputs'][prior_path.name])
    if not receipt_valid(engine.PRIOR, 'native-cal500-raw-activity', digest(engine.PRIOR / 'plan.json')):
        raise ValueError('Prior Focus model receipt not verified')
    names = ['Usage-Reading', 'Usage-Studying']
    save(directory / 'inputs/prior-focus-heads.json', {'target_names': names,
         'heads': [prior['heads'][prior['target_names'].index(name)] for name in names],
         'source_path': str(prior_path), 'source_sha256': digest(prior_path)})
    audit = {}
    for regime, sources in SOURCE_SETS.items():
        target_rows = [row for t in TARGET_SETS[regime]
                       for row in load(directory / f'inputs/{t}.json')['rows']]
        for source in sources:
            metadata = load(directory / f'inputs/{source}.json')
            mask, details = exclusion(metadata['rows'], target_rows)
            groups = np.asarray(metadata['groups'])[mask]
            if mask.sum() < 30 or len(set(groups)) < 3:
                raise ValueError('Insufficient disjoint source: ' + source)
            audit[regime + '__' + source] = details
            npz(directory / f'inputs/selection-{regime}__{source}.npz', retained=mask,
                split=folds(groups, count=3, seed=engine.SEED + 1))
    save(directory / 'inputs/exclusions.json', audit)
    code = codes()
    for relative, expected in code.items():
        destination = directory / 'source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXPERIMENTS / relative, destination)
        if digest(destination) != expected:
            raise ValueError('Code changed during freeze')
    plan = {'format': 'external-common-target-candidates-v1', 'created_at': stamp(),
            'code': code, 'versions': versions(), 'references': references,
            'inputs': {str(p.relative_to(directory)): digest(p) for p in sorted((directory / 'inputs').iterdir())},
            'grid_soundtracks': engine.catalogue(),
            'grid_pilots': [{'features': 'embedding', 'learner': spec}
                           for spec in [{'family': 'mean'}, {'family': 'median'}, *engine.learner.grid()]],
            'source_sets': SOURCE_SETS, 'target_sets': TARGET_SETS,
            'primary_pilot_aggregation': 'mean_native_then_map',
            'secondary_aggregations': ['mean_embedding_then_predict', 'mean_mapped_chunks'],
            'ensemble': 'equal PMEmo/DEAM, fixed before target scoring; primary aggregation only',
            'focus_bootstrap': {'replicates': 1000, 'seed': SEED, 'stratify': 'existing outer fold, sample whole groups'},
            'embedding_signature': original['embedding_signature'],
            'local_labels_used': False, 'target_labels_used_for_selection': False,
            'limits': ['Reused corpora, exploratory rather than pristine confirmation.',
                       'Pilot cache has embeddings only; acoustic activation is tested on soundtrack excerpts.',
                       'Unknown artists, aliases and upstream training overlap remain unresolved.',
                       'No target-fitted calibration, product adoption or consumer/listening acceptance.']}
    save(directory / 'plan.json', plan)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
        'pilot_rows': {t: len(load(directory / f'inputs/{t}.json')['rows']) for t in PILOTS},
        'source_training_rows': {k: v['training_rows'] for k, v in audit.items()}}), flush=True)
    return plan


def validate(directory, *, execution=True):
    plan = load(directory / 'plan.json')
    if execution and (plan['code'] != codes() or plan['versions'] != versions()):
        raise ValueError('Candidate code/runtime changed; preserve this round')
    for prefix, entries in (('', plan['inputs']), ('source/', plan['code'])):
        for relative, expected in entries.items():
            path = inside(directory / (prefix + relative))
            if not path.is_relative_to(directory) or digest(path) != expected:
                raise ValueError('Frozen candidate input/archive changed: ' + relative)
    for path, expected in plan['references'].items():
        if digest(inside(path)) != expected:
            raise ValueError('Referenced evidence changed: ' + path)
    return plan


def score_stage(directory, regime, source):
    plan = load(directory / 'plan.json')
    candidates = plan['grid_' + regime]
    meta = load(directory / f'inputs/{source}.json')
    data = input_arrays(directory / f'inputs/{source}.npz')
    split = input_arrays(directory / f'inputs/selection-{regime}__{source}.npz')
    training, y = engine.subset(data, split['retained']), data['y'][split['retained']]
    rules = load(directory / 'inputs/rules.json')
    supported = {v: rule for v, rule in rules[source].items() if v in engine.VIBES
                 and any(v in rules[t] for t in TARGET_SETS[regime])}
    pool = engine.pool_oof(training, y, split['split'], candidates)
    selected = engine.selection(y, pool, split['split'], meta['target_names'], supported, candidates)
    fitted = engine.fit_choices(training, y, engine.required_ids(selected), candidates)
    arrays, reports = {}, {}
    for target in TARGET_SETS[regime]:
        target_meta = load(directory / f'inputs/{target}.json')
        test = input_arrays(directory / f'inputs/{target}.npz')
        if regime == 'pilots':
            x = {'embedding': test['embedding'], 'attributes': np.empty((len(test['embedding']), 0))}
            raw = engine.predict_choices(fitted, candidates, x)
            averaged = {i: average_chunks(v, test['offsets']) for i, v in raw.items()}
            composed, _ = engine.composed_predictions(selected, averaged, (len(test['y']), y.shape[1]), supported, meta['target_names'])
            policies = {'mean_native_then_map': composed}
            chunks, _ = engine.composed_predictions(selected, raw, (len(test['embedding']), y.shape[1]), supported, meta['target_names'])
            policies['mean_mapped_chunks'] = {v: {m: average_chunks(p, test['offsets']) for m, p in methods.items()} for v, methods in chunks.items()}
            mean_features = {'embedding': average_chunks(test['embedding'], test['offsets']),
                             'attributes': np.empty((len(test['y']), 0))}
            values = engine.predict_choices(fitted, candidates, mean_features)
            policies['mean_embedding_then_predict'], _ = engine.composed_predictions(selected, values, (len(test['y']), y.shape[1]), supported, meta['target_names'])
        else:
            values = engine.predict_choices(fitted, candidates, test)
            composed, _ = engine.composed_predictions(selected, values, (len(test['y']), y.shape[1]), supported, meta['target_names'])
            policies = {'aligned_excerpt': composed}
        report = {'rows': len(test['y']), 'groups': len(set(target_meta['groups'])), 'policies': {}}
        arrays[target + '__outer'] = test['outer']
        for policy, composed in policies.items():
            report['policies'][policy] = {}
            for vibe, methods in composed.items():
                if vibe not in rules[target]:
                    continue
                observed = apply(rules[target][vibe]['expression'], test['y'], target_meta['target_names'])
                arrays[target + '__' + vibe + '__observed'] = observed
                report['policies'][policy][vibe] = {}
                for method, values in methods.items():
                    result = summary(observed, values, test['outer'])
                    result.update(score_mean=float(values.mean()), reference_mean=float(observed.mean()))
                    report['policies'][policy][vibe][method] = result
                    arrays[target + '__' + policy + '__' + vibe + '__' + method] = values
        if regime == 'soundtracks':
            # Same actual rows and each expert's selected score, compared with
            # both independently rated constructs. These do not select models.
            report['construct_cross_responses'] = {}
            for vibe in ('energize', 'release'):
                if vibe in composed:
                    report['construct_cross_responses'][vibe] = {name: summary(
                        test['y'][:, target_meta['target_names'].index(name)], composed[vibe]['selected'], test['outer'])
                        for name in ('energy', 'anger')}
        reports[target] = report
    prefix = directory / (regime + '-' + source)
    experts = {v: engine.expert_record(source, v, fitted[methods['selected']], methods['selected'], candidates,
               meta['target_names'], supported[v], digest(directory / 'plan.json')) for v, methods in selected['vibes'].items()}
    save(prefix.with_suffix('.report.json'), {'source': source, 'regime': regime, 'selection': selected,
         'exclusions': load(directory / 'inputs/exclusions.json')[regime + '__' + source],
         'target_labels_used_for_selection': False, 'targets': reports})
    save(prefix.with_suffix('.experts.json'), experts)
    npz(prefix.with_suffix('.predictions.npz'), **arrays)
    return [prefix.with_suffix(s) for s in ('.report.json', '.experts.json', '.predictions.npz')]


def focus_stage(directory):
    plan = load(directory / 'plan.json')
    data = input_arrays(directory / 'inputs/cal500-raw-activity.oof.npz')
    metadata = load(directory / 'inputs/cal500-raw-activity.json')
    observed, split = data['concentrate__observed'], data['outer']
    methods = ['prior_embedding_mae', 'selected', 'shared', 'native_mse']
    report = {'rows': len(observed), 'groups': len(set(metadata['groups'])),
              'methods': {m: summary(observed, data['concentrate__' + m], split) for m in methods},
              'paired_ndcg_difference_against_prior': {}, 'new_model_selected_from_outer_results': False}
    group_array = np.asarray(metadata['groups'])
    bootstrap = {m: [] for m in methods[1:]}
    rng = np.random.default_rng(plan['focus_bootstrap']['seed'])
    by_fold = [[np.flatnonzero((split == fold) & (group_array == group))
                for group in sorted(set(group_array[split == fold]))] for fold in sorted(set(split))]
    for _ in range(plan['focus_bootstrap']['replicates']):
        indices, boot_folds = [], []
        for fold, blocks in enumerate(by_fold):
            selected = np.concatenate([blocks[i] for i in rng.integers(len(blocks), size=len(blocks))])
            indices.extend(selected)
            boot_folds.extend([fold] * len(selected))
        indices, boot_folds = np.asarray(indices), np.asarray(boot_folds)
        base = summary(observed[indices], data['concentrate__prior_embedding_mae'][indices], boot_folds)['ndcg']
        for method in bootstrap:
            value = summary(observed[indices], data['concentrate__' + method][indices], boot_folds)['ndcg']
            bootstrap[method].append(value - base)
    for method, values in bootstrap.items():
        report['paired_ndcg_difference_against_prior'][method] = {
            'point': report['methods'][method]['ndcg'] - report['methods']['prior_embedding_mae']['ndcg'],
            'conditional_group_bootstrap_95_percentile': np.quantile(values, [.025, .975]).tolist()}
    prior = load(directory / 'inputs/prior-focus-heads.json')
    x = input_arrays(directory / 'inputs/cal500-raw-activity.npz')['embedding']
    first = np.column_stack([inference.replay(head, x)[:, 0] for head in prior['heads']]).mean(1)
    second = np.column_stack([engine.learner.replay(head, x)[:, 0] for head in prior['heads']]).mean(1)
    error = float(np.max(np.abs(first - second)))
    if error > 1e-10:
        raise ValueError('Prior Focus export replay differs')
    report['prior_export_replay_max_absolute_error'] = error
    report['limits'] = 'Fixed-model conditional bootstrap on reused CAL500 OOF; no independent new Focus confirmation corpus or listening acceptance.'
    save(directory / 'focus.report.json', report)
    save(directory / 'focus-candidate.json', {**prior, 'vibe': 'concentrate', 'combine': 'equal mean Reading/Studying',
         'family': 'embedding', 'plan_sha256': digest(directory / 'plan.json'), 'local_labels_used': False, 'product_adoption': False})
    return [directory / 'focus.report.json', directory / 'focus-candidate.json']


def report_stage(directory):
    report = {'plan_sha256': digest(directory / 'plan.json'), 'sources': {}, 'pilot_va_ensembles': {},
              'focus': load(directory / 'focus.report.json'), 'local_labels_used': False, 'state': 'complete'}
    for regime, sources in SOURCE_SETS.items():
        report['sources'][regime] = {s: load(directory / f'{regime}-{s}.report.json')['targets'] for s in sources}
    first, second = [input_arrays(directory / f'pilots-{s}.predictions.npz') for s in ('pmemo', 'deam')]
    for target in PILOTS:
        report['pilot_va_ensembles'][target] = {}
        for vibe in ('relax', 'cruise', 'energize', 'uplift'):
            key = target + '__mean_native_then_map__' + vibe + '__selected'
            observed, split = target + '__' + vibe + '__observed', target + '__outer'
            if not np.array_equal(first[observed], second[observed]) or not np.array_equal(first[split], second[split]):
                raise ValueError('Pilot ensemble target rows changed')
            values = (first[key] + second[key]) / 2
            report['pilot_va_ensembles'][target][vibe] = summary(first[observed], values, first[split])
    save(directory / 'summary.json', report)
    print(json.dumps({'complete': str(directory), 'source_fits': 8, 'common_target_directions': 14,
                     'focus': report['focus']['paired_ndcg_difference_against_prior']}), flush=True)
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
            raise SystemExit('This candidate round is already running') from error
        if args.action == 'freeze':
            freeze(directory)
            return
        # Permit read-only evidence verification after the entrypoint evolves;
        # fitting still requires the exact frozen executable code/runtime.
        validate(directory, execution=args.action != 'verify')
        names = [regime + '-' + s for regime, sources in SOURCE_SETS.items() for s in sources] + ['focus', 'summary']
        if args.action == 'verify':
            if not all(receipt_valid(directory, name, digest(directory / 'plan.json')) for name in names):
                raise ValueError('Candidate stages incomplete')
            print(json.dumps({'verified_stages': len(names), 'state': 'complete'}), flush=True)
            return
        with threadpool_limits(limits=1):
            for regime, sources in SOURCE_SETS.items():
                for source in sources:
                    stage(directory, regime + '-' + source, lambda r=regime, s=source: score_stage(directory, r, s))
            stage(directory, 'focus', lambda: focus_stage(directory))
            stage(directory, 'summary', lambda: report_stage(directory))
        validate(directory)


if __name__ == '__main__':
    main()
