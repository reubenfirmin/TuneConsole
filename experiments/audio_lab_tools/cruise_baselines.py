"""Freeze the enlarged local corpus and refresh shared-model comparators.

External annotations are the main reference. New local identity groups stay
outside every fit; their supplementary outcomes are opened only after exports.
All artifacts stay in experiments/. The application model is never replaced.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3

import numpy as np
from threadpoolctl import threadpool_limits

from . import candidate_followup as followup
from . import cruise_specialist as specialist
from . import external_training as training
from . import mapped_vibe_rules as rules
from . import train_vibes
from .external_binary_calibration import metrics as local_metrics
from .external_experiment import LAB, digest, save, stamp
from .external_transfer import SNAPSHOT, score as choice_metrics, select_vibe_alpha
from .mapped_vibe_metrics import summary
from .product_baseline_audit import DEFAULT as PRODUCT, predict
from .transfer_identity import exclusion
from .vibe_specialist_round import EXPERIMENTS, input_arrays, inside, load, versions

DEFAULT = LAB / 'local/mapped-vibes-20261029/cruise-baselines-1'
PACKAGE = LAB / 'local/mapped-vibes-20261029/cruise-package-2'
TARGETS = (*followup.PILOTS, 'soundtracks-all', 'cal500-raw-activity')
METHODS = ('checkpoint', 'refresh_calibration', 'refresh_both')


def identities(rows):
    return [{**row, 'id': row.get('id') or row['video_id'],
             'group': row.get('group') or row.get('id') or row['video_id']} for row in rows]


def reserve_groups(rows, prior_ids):
    """Do not split any known collaborator/recording component across fits."""
    groups = training.validation_groups(identities(rows))
    additions = {row['video_id'] for row in rows if row['video_id'] not in prior_ids}
    reserved = {group for row, group in zip(rows, groups, strict=True) if row['video_id'] in additions}
    development = [i for i, group in enumerate(groups) if group not in reserved]
    held = [i for i, group in enumerate(groups) if group in reserved]
    return development, held, groups, additions


def code_paths():
    paths = [Path(__file__), Path(train_vibes.__file__), Path(training.__file__),
             Path(specialist.__file__), Path(followup.__file__), Path(rules.__file__),
             Path(__file__).with_name('external_transfer.py'),
             Path(__file__).with_name('external_secondary.py'),
             Path(__file__).with_name('transfer_identity.py'),
             Path(__file__).with_name('mapped_vibe_metrics.py'),
             Path(__file__).with_name('product_baseline_audit.py'),
             Path(__file__).with_name('external_binary_calibration.py'),
             Path(__file__).with_name('external_ranking.py'), EXPERIMENTS / 'run.py']
    result = {str(p.relative_to(EXPERIMENTS)): p for p in paths}
    result.update({'product/' + p.name: p for p in (Path(specialist.shared.__file__),
                  Path(specialist.audio_embedding.__file__))})
    return result


def freeze(directory, database):
    from yt_playlist.core import sample_files
    from .mapped_vibe_round import PRIOR
    if directory.exists():
        raise ValueError('Preserve previous evidence; choose a new --directory')
    followup.validate(followup.DEFAULT, execution=False)
    package_receipt = load(PACKAGE / 'receipt.json')
    if package_receipt['outputs']['cruise-specialist.json'] != digest(PACKAGE / 'cruise-specialist.json'):
        raise ValueError('Cruise export changed since verification')
    specialist.load(PACKAGE / 'cruise-specialist.json')
    product = load(PRODUCT / 'product-model.json')
    prior_path = PRIOR / 'inputs/local.json'
    prior = load(prior_path)
    historical = load(SNAPSHOT)
    protected = prior['protected_identities']
    prior_ids = {row['video_id'] for row in protected}
    records, skipped = train_vibes.examples_from_db(database, require_embeddings=True)
    rows = sorted([row for row in records if row['embedding_signature'] == product['embedding_signature']],
                  key=lambda row: row['video_id'])
    development, held, groups, additions = reserve_groups(rows, prior_ids)
    if len(development) < 20 or len(set(groups[i] for i in development)) < 5 or not held:
        raise ValueError('Insufficient development groups or no reserved additions')
    directory.mkdir(parents=True)
    (directory / 'inputs/samples').mkdir(parents=True)
    references = {str(p): digest(p) for p in (prior_path, SNAPSHOT, PRODUCT / 'product-model.json',
                  PACKAGE / 'cruise-specialist.json', PACKAGE / 'receipt.json')}
    # Reopen read-only only to locate immutable excerpt files selected by the
    # export's own transaction. Verify identity, provenance and pooled features.
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        files = dict(conn.execute('SELECT id,filename FROM sample_attributes'))
    for row in rows:
        samples, relative = [], []
        for sample_id in row['sample_ids']:
            source = Path(files[sample_id])
            value = sample_files.read(source, expected={'id': sample_id, 'video_id': row['video_id']})
            key = digest(source)
            destination = directory / 'inputs/samples' / (key + ''.join(source.suffixes))
            if not destination.exists():
                shutil.copyfile(source, destination)
            if digest(destination) != key:
                raise ValueError('Excerpt changed while freezing')
            samples.append(value)
            relative.append(str(destination.relative_to(directory)))
        if (any(specialist.audio_embedding.signature(v) != product['embedding_signature'] for v in samples)
                or not np.allclose(specialist.audio_embedding.vector(samples), row['embedding_values'], atol=1e-12, rtol=0)):
            raise ValueError('Selected excerpt files do not reproduce the frozen example')
        row['sample_files'] = relative
        row.pop('feedback_prediction', None)
    current = identities(rows)
    exposure = {}
    for name, previous in (('previous_protected', protected), ('original_gold', historical['gold']),
                           ('original_silver', historical['silver'])):
        mask, audit = exclusion(current, identities(previous))
        exposure[name] = {'matched_ids': [r['video_id'] for r, keep in zip(rows, mask, strict=True) if not keep],
                          'audit': audit}
    lookup = {r['video_id']: r for r in [*protected, *historical['gold'], *historical['silver']]}
    teacher = [lookup.get(key, {'video_id': key}) for key in historical['teacher']['training_video_ids']]
    mask, audit = exclusion(current, identities(teacher))
    exposure['original_teacher_training'] = {'matched_ids': [r['video_id'] for r, keep in zip(rows, mask, strict=True) if not keep],
                                              'audit': audit}
    for source in specialist.SOURCES:
        path = followup.DEFAULT / f'inputs/{source}.json'
        data_path = followup.DEFAULT / f'inputs/selection-pilots__{source}.npz'
        native = load(path)
        keep = input_arrays(data_path)['retained']
        fitted_rows = [row for row, retained in zip(native['rows'], keep, strict=True) if retained]
        mask, audit = exclusion(current, fitted_rows)
        exposure['cruise_native_' + source] = {'matched_ids': [r['video_id'] for r, retained in zip(rows, mask, strict=True) if not retained],
                                              'audit': audit}
        references.update({str(p): digest(p) for p in (path, data_path)})
    save(directory / 'inputs/development.json', {'rows': [rows[i] for i in development],
         'groups': [groups[i] for i in development]})
    save(directory / 'inputs/reserved.json', {'rows': [rows[i] for i in held],
         'groups': [groups[i] for i in held]})
    prior_gold = {r['video_id']: r for r in historical['gold']}
    revisions = [r['video_id'] for r in rows if r['video_id'] in prior_gold
                 and set(r['labels']) != set(prior_gold[r['video_id']]['labels'])]
    save(directory / 'inputs/inventory.json', {'created_at': stamp(), 'database': str(database.resolve()),
         'access': 'read-only export; no Store, migrations, predictions-as-targets or live writes',
         'usable_rows': len(rows), 'skipped': skipped,
         'incompatible_rows': len(records) - len(rows), 'previous_protected_ids': len(prior_ids),
         'new_ids': sorted(additions), 'development_rows': len(development), 'reserved_rows': len(held),
         'reserved_older_group_members': [rows[i]['video_id'] for i in held if rows[i]['video_id'] not in additions],
         'label_revisions_since_original_gold': revisions, 'exposure': exposure,
         'limitations': ['Unresolved aliases and missing artists can hide identity or upstream training overlap.',
                        'Exposure audits describe the recorded original gold/silver/teacher and exact exported native training cohorts, not every historical experiment.']})
    # Freeze the existing external rows, annotations, identity masks and rules.
    targets = {}
    for target in TARGETS:
        source = followup.DEFAULT / f'inputs/{target}.json'
        arrays = followup.DEFAULT / f'inputs/{target}.npz'
        metadata = load(source)
        references.update({str(p): digest(p) for p in (source, arrays)})
        targets[target] = metadata
    native_meta = load(followup.DEFAULT / 'inputs/emotify-artist.json')
    native_data = input_arrays(followup.DEFAULT / 'inputs/emotify-artist.npz')
    if native_meta['target_names'] != list(specialist.shared.EMOTIONS):
        raise ValueError('Refreshed native targets do not match the shared architecture')
    target_rows = [row for value in targets.values() for row in value['rows']]
    keep, audit = exclusion(native_meta['rows'], [*current, *target_rows])
    if keep.sum() < 30:
        raise ValueError('Too few native Emotify rows after evaluation identity exclusions')
    np.savez_compressed(directory / 'inputs/native.npz', embedding=native_data['embedding'][keep], y=native_data['y'][keep])
    save(directory / 'inputs/native.json', {'target_names': native_meta['target_names'],
         'rows': [r for r, selected in zip(native_meta['rows'], keep, strict=True) if selected],
         'groups': np.asarray(native_meta['groups'])[keep].tolist(), 'exclusions': audit})
    references.update({str(p): digest(p) for p in (followup.DEFAULT / 'inputs/emotify-artist.json',
                       followup.DEFAULT / 'inputs/emotify-artist.npz')})
    shutil.copyfile(PRODUCT / 'product-model.json', directory / 'checkpoint.json')
    shutil.copyfile(PACKAGE / 'cruise-specialist.json', directory / 'cruise-specialist.json')
    paths = code_paths()
    for relative, source in paths.items():
        destination = directory / 'source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    inputs = {str(p.relative_to(directory)): digest(p) for p in directory.joinpath('inputs').rglob('*') if p.is_file()}
    save(directory / 'plan.json', {'created_at': stamp(), 'runtime': versions(),
         'code': {relative: digest(p) for relative, p in paths.items()}, 'inputs': inputs, 'references': references,
         'artifacts': {name: digest(directory / name) for name in ('checkpoint.json', 'cruise-specialist.json')},
         'methods': list(METHODS), 'native_grid': list(training.ALPHAS), 'calibration_grid': list(training.ALPHAS),
         'selection': 'Native alpha: three native grouped folds, mean absolute native annotation error. Calibration alpha: three development grouped folds, acceptable-label log loss. Five outer development folds diagnose the calibration selection procedure.',
         'reserve': 'All current identity components containing an ID absent from the previous protected snapshot; reserved JSON is not a fit input. Artifacts fixed before evaluation opens reserved outcomes.',
         'external_targets': list(TARGETS), 'annotation_rules': rules.registry(), 'bootstrap_draws': 1000,
         'bootstrap_seed': 571570, 'policy': 'Compare both refreshed variants and fixed product on identical externally annotated rows; no target-label model selection, automatic promotion or new individual models for other vibes.',
         'limits': ['External cohorts and development labels have been examined previously.',
                    'Refreshed calibration uses one-hot fractions for acceptable alternatives, preserving the existing ridge architecture.',
                    'Frozen checkpoint has in-sample development rows; its development fit is not a generalization estimate.',
                    'Reserved developer annotations are supplementary and must be split by known prior exposure.']})
    print(json.dumps({'state': 'frozen', 'directory': str(directory), 'rows': len(rows),
          'development': len(development), 'reserved': len(held), 'new_ids': len(additions),
          'native_rows': int(keep.sum()), 'label_revisions': len(revisions),
          'new_id_exposure': {name: len(additions & set(value['matched_ids'])) for name, value in exposure.items()}}), flush=True)


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    for relative, expected in {**plan['inputs'], **plan['artifacts']}.items():
        path = inside(directory / relative)
        if not path.is_relative_to(directory) or digest(path) != expected:
            raise ValueError('Baseline frozen input changed: ' + relative)
    for path, expected in plan['references'].items():
        if digest(inside(path)) != expected:
            raise ValueError('Baseline reference changed: ' + path)
    current = code_paths()
    for relative, expected in plan['code'].items():
        if digest(directory / 'source' / relative) != expected or (execution and digest(current[relative]) != expected):
            raise ValueError('Baseline code differs from its frozen plan: ' + relative)
    return plan


def stage(stage):
    return {key: stage[key].tolist() for key in ('mean', 'scale', 'weights', 'intercept')}


def fit(directory):
    validate(directory, execution=True)
    receipt = directory / 'fit.receipt.json'
    if receipt.exists() or any(directory.glob('refresh_*.json')):
        raise ValueError('Fits already exported; preserve these coefficients')
    product = load(directory / 'checkpoint.json')
    development = load(directory / 'inputs/development.json')
    rows, groups = development['rows'], development['groups']
    x = np.asarray([r['embedding_values'] for r in rows])
    y = np.asarray([[int(v in r['labels']) / len(r['labels']) for v in rules.VIBES] for r in rows])
    native = input_arrays(directory / 'inputs/native.npz')
    native_meta = load(directory / 'inputs/native.json')
    alpha = training.choose_alpha(native['embedding'], native['y'], native_meta['groups'], list(range(9)))
    refitted = training.fit_path(native['embedding'], native['y'], [alpha])[0]
    outputs, diagnostics = {}, {}
    outer = training.folds(groups)
    for method in METHODS[1:]:
        head = ({'medians': np.asarray(product['native']['mean']),
                 **{k: np.asarray(v) for k, v in product['native'].items()}} if method == 'refresh_calibration' else refitted)
        features = training.predict(head, x)
        oof = np.zeros_like(y)
        selected = []
        for fold in range(5):
            train, test = outer != fold, outer == fold
            penalty = select_vibe_alpha(features[train], y[train], np.asarray(groups)[train].tolist())
            oof[test] = training.predict(training.fit_path(features[train], y[train], [penalty])[0], features[test])
            selected.append(penalty)
        penalty = select_vibe_alpha(features, y, groups)
        calibration = training.fit_path(features, y, [penalty])[0]
        artifact = {**product, 'native': stage(head), 'calibration': stage(calibration),
                    'training': {'songs': len(rows), 'native_songs': product['training']['native_songs'] if method == 'refresh_calibration' else len(native['y']),
                                 'native_alpha': product['training']['native_alpha'] if method == 'refresh_calibration' else alpha,
                                 'calibration_alpha': penalty, 'objective': 'acceptable_nll'},
                    'evaluation': {'status': 'experiments_only_refreshed_comparator', 'plan_sha256': digest(directory / 'plan.json')},
                    'provenance': {'plan_sha256': digest(directory / 'plan.json'), 'method': method,
                                   'checkpoint_sha256': digest(directory / 'checkpoint.json')}}
        specialist.shared.validate(artifact)
        destination = directory / (method + '.json')
        save(destination, artifact)
        outputs[destination.name] = digest(destination)
        np.savez_compressed(directory / (method + '.oof.npz'), observed=y, predicted=oof, outer=outer)
        outputs[method + '.oof.npz'] = digest(directory / (method + '.oof.npz'))
        diagnostics[method] = {'calibration_alpha': penalty, 'outer_alphas': selected,
                               'development_oof': local_metrics(y, oof)}
        # Exact scalar replay from the real shared runtime, all development rows.
        for values, expected in zip(x, predict(artifact, x), strict=True):
            emotion = specialist.shared._predict(artifact['native'], values.tolist())
            scalar = np.maximum(specialist.shared._predict(artifact['calibration'], emotion), 1e-8)
            if not np.allclose(expected, scalar / scalar.sum(), atol=1e-12, rtol=0):
                raise ValueError('Refreshed shared artifact differs from scalar product inference')
    save(directory / 'fit.report.json', {'created_at': stamp(), 'native_alpha': alpha, 'methods': diagnostics,
         'reserved_outcomes_read': False, 'scope': 'Shared ridge architecture, development-only calibration and independent scalar export replay.'})
    outputs['fit.report.json'] = digest(directory / 'fit.report.json')
    validate(directory, execution=True)
    save(receipt, {'plan_sha256': digest(directory / 'plan.json'), 'outputs': outputs})
    print(json.dumps({'state': 'fitted', 'native_alpha': alpha,
          'methods': {name: {'calibration_alpha': value['calibration_alpha'],
                            'oof_top1': value['development_oof']['top_choice']['top_1_acceptable']}
                      for name, value in diagnostics.items()}, 'reserved_outcomes_read': False}), flush=True)


def interval(y, baseline, candidate, split, groups, draws, seed):
    """Paired whole-group draws inside each original fold, fixed coefficients."""
    rng = np.random.default_rng(seed)
    members = []
    groups = np.asarray(groups)
    if any(len(set(split[groups == key])) != 1 for key in set(groups)):
        raise ValueError('Identity group crosses evaluation folds')
    for fold in sorted(set(split)):
        keys = sorted(set(groups[split == fold]))
        members.append([np.flatnonzero((groups == key) & (split == fold)) for key in keys])
    observed = summary(y, candidate, split)['ndcg'] - summary(y, baseline, split)['ndcg']
    values = []
    for _ in range(draws):
        index = np.concatenate([np.concatenate([part[i] for i in rng.integers(len(part), size=len(part))]) for part in members])
        before, after = summary(y[index], baseline[index], split[index]), summary(y[index], candidate[index], split[index])
        if before['ndcg'] is not None and after['ndcg'] is not None:
            values.append(after['ndcg'] - before['ndcg'])
    return {'ndcg_gain': observed, 'conditional_95_percent_interval': np.quantile(values, [.025, .975]).tolist() if values else None,
            'usable_draws': len(values), 'draws': draws,
            'policy': 'Paired identity-group bootstrap within fixed folds; coefficients, selection and prior inspection held fixed. No multiple-comparison correction.'}


def evaluate(directory):
    plan = validate(directory, execution=True)
    receipt = load(directory / 'fit.receipt.json')
    if receipt['plan_sha256'] != digest(directory / 'plan.json'):
        raise ValueError('Fit receipt does not match plan')
    for relative, expected in receipt['outputs'].items():
        if digest(directory / relative) != expected:
            raise ValueError('Refreshed artifact changed')
    if (directory / 'evaluation.receipt.json').exists():
        raise ValueError('Reserved outcomes already scored; preserve the saved evaluation')
    models = {name: load(directory / ('checkpoint.json' if name == 'checkpoint' else name + '.json')) for name in METHODS}
    development = load(directory / 'inputs/development.json')['rows']
    historical = load(SNAPSHOT)
    native_meta = load(followup.DEFAULT / 'inputs/emotify-artist.json')
    # All controls use an identical primary target slice, disjoint from the
    # union of known fixed and refreshed shared training identities.
    union = [*native_meta['rows'], *identities(historical['gold']), *identities(development)]
    cases, arrays = {}, {}
    for target in TARGETS:
        meta = load(followup.DEFAULT / f'inputs/{target}.json')
        data = input_arrays(followup.DEFAULT / f'inputs/{target}.npz')
        x = followup.average_chunks(data['embedding'], data['offsets']) if target in followup.PILOTS else data['embedding']
        keep, audit = exclusion(meta['rows'], union)
        predictions = {name: predict(model, x) for name, model in models.items()}
        observed_rules = rules.registry()['pmemo' if target in followup.PILOTS else target]
        cases[target] = {'rows': len(x), 'shared_training_disjoint_rows': int(keep.sum()), 'exclusion': audit, 'vibes': {}}
        for vibe in rules.VIBES:
            if vibe not in observed_rules:
                continue
            y = rules.apply(observed_rules[vibe]['expression'], data['y'], meta['target_names'])
            scores = {name: values[:, rules.VIBES.index(vibe)] for name, values in predictions.items()}
            if target in followup.PILOTS and vibe == 'cruise':
                scores['cruise_specialist'] = np.asarray([specialist.score(specialist.load(directory / 'cruise-specialist.json'), data['embedding'][left:right].tolist())['score']
                        for left, right in zip(data['offsets'][:-1], data['offsets'][1:], strict=True)])
            split, groups = data['outer'][keep], np.asarray(meta['groups'])[keep]
            if keep.sum() < 10 or not len(set(split)) == 5:
                raise ValueError('Insufficient disjoint evaluation rows')
            case = {'semantics': observed_rules[vibe]['semantics'], 'metrics': {name: summary(y[keep], values[keep], split) for name, values in scores.items()}, 'paired': {}}
            for name in scores:
                if name != 'checkpoint':
                    case['paired'][name + '_vs_checkpoint'] = interval(y[keep], scores['checkpoint'][keep], scores[name][keep], split, groups,
                                                                      plan['bootstrap_draws'], plan['bootstrap_seed'])
            if 'cruise_specialist' in scores:
                for name in METHODS[1:]:
                    case['paired']['cruise_specialist_vs_' + name] = interval(y[keep], scores[name][keep], scores['cruise_specialist'][keep], split, groups,
                                                                             plan['bootstrap_draws'], plan['bootstrap_seed'])
            cases[target]['vibes'][vibe] = case
            arrays[target + '__' + vibe + '__observed'] = y
            for name, values in scores.items():
                arrays[target + '__' + vibe + '__' + name] = values
        arrays[target + '__retained'] = keep
        print(json.dumps({'external_target': target, 'state': 'complete', 'rows': int(keep.sum())}), flush=True)
    # Every artifact and external comparison is fixed before opening outcomes.
    from yt_playlist.core import sample_files
    held = load(directory / 'inputs/reserved.json')
    inventory = load(directory / 'inputs/inventory.json')
    rows = held['rows']
    x = np.asarray([r['embedding_values'] for r in rows])
    y = np.asarray([[int(v in r['labels']) for v in rules.VIBES] for r in rows])
    local = {name: predict(model, x) for name, model in models.items()}
    for name in METHODS:
        hybrid = local[name].copy()
        for i, row in enumerate(rows):
            samples = [sample_files.read(directory / p) for p in row['sample_files']]
            value = specialist.route_samples(models[name], samples, specialist_path=directory / 'cruise-specialist.json', mode='active')
            if value['applied_vibes'] != ['cruise']:
                raise ValueError('Reserved capture failed specialist routing')
            hybrid[i, rules.VIBES.index('cruise')] = value['suitability']['cruise']
        local[name + '_with_cruise'] = hybrid
    exposed = set().union(*(set(value['matched_ids']) for value in inventory['exposure'].values()))
    additions = set(inventory['new_ids'])
    slices = {'all_reserved_group_members': np.ones(len(rows), dtype=bool),
              'new_ids': np.asarray([r['video_id'] in additions for r in rows]),
              'new_ids_no_recorded_exposure': np.asarray([r['video_id'] in additions and r['video_id'] not in exposed for r in rows])}
    local_report = {}
    for name, mask in slices.items():
        if not mask.any():
            local_report[name] = {'rows': 0, 'methods': {}}
            continue
        # Hybrid scores are valid only for selected-vibe retrieval, so report
        # per-vibe AP; retain shared-model top choices separately.
        local_report[name] = {'rows': int(mask.sum()), 'methods': {key: {
            'per_vibe': local_metrics(y[mask], values[mask])['per_vibe'],
            **({'shared_top_choice': choice_metrics(y[mask], values[mask])} if key in METHODS else {})}
            for key, values in local.items()}}
    report = {'created_at': stamp(), 'plan_sha256': digest(directory / 'plan.json'), 'cases': cases,
              'local_supplement': local_report, 'shared_model_choice': 'No automatic promotion. Existing validated checkpoint remains the serving comparator pending consumer and acceptance checks.',
              'limits': plan['limits'], 'hybrid_policy': 'Cruise override only for within-vibe suitability. All five other scores and shared overall classification retained.'}
    save(directory / 'evaluation.report.json', report)
    np.savez_compressed(directory / 'external-predictions.npz', **arrays)
    validate(directory, execution=True)
    save(directory / 'evaluation.receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
         'fit_receipt_sha256': digest(directory / 'fit.receipt.json'), 'outputs': {name: digest(directory / name)
         for name in ('evaluation.report.json', 'external-predictions.npz')}})
    print(json.dumps({'state': 'evaluated', 'external_targets': len(cases), 'reserved_rows': len(rows),
                      'new_ids_no_recorded_exposure': local_report['new_ids_no_recorded_exposure']['rows']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'fit', 'evaluate', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--database', type=Path, default=Path('/home/rfirmin/.local/share/yt-playlist/state.db'))
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory, args.database)
        elif args.action == 'fit':
            fit(directory)
        elif args.action == 'evaluate':
            evaluate(directory)
        else:
            validate(directory)
            for name in ('fit.receipt.json', 'evaluation.receipt.json'):
                if (directory / name).exists():
                    for relative, expected in load(directory / name)['outputs'].items():
                        if digest(directory / relative) != expected:
                            raise ValueError('Baseline stage output changed')
            print(json.dumps({'state': 'verified', 'directory': str(directory)}), flush=True)


if __name__ == '__main__':
    main()
