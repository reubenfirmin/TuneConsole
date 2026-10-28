"""Test native VA predictors on entirely different annotated audio corpora."""
import json

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, save, stamp
from .external_learners import candidates, load, prediction, select, serializable
from .external_secondary import artist_keys, identity
from .external_training import bootstrap_improvement, choose_alpha, fit_path, metrics, predict

TARGETS = ['valence', 'arousal']


def exclude_overlap(source, target):
    artists = {key for row in target for key in artist_keys(row.get('artist', '').split('+')[0])}
    titles = {identity(row['title']) for row in target if row.get('title')}
    hashes = {row['audio_sha256'] for row in target}
    return np.array([not (artist_keys(row.get('artist', '').split('+')[0]) & artists
                         or identity(row.get('title', '')) in titles
                         or row['audio_sha256'] in hashes) for row in source])


def evaluate(sources=('pmemo', 'deam', 'amg1608-matched', 'ntwicm-matched')):
    with threadpool_limits(limits=1):
        corpora = {}
        for name in sources:
            if not (OUT / f'{name}.features.json').exists():
                continue
            rows, y, names, groups, features, provenance = load(name, ())
            columns = [names.index(target) for target in TARGETS]
            corpora[name] = (rows, y[:, columns].astype(float), groups, features, provenance)
        report = {'created_at': stamp(), 'sources': list(corpora), 'directions': {},
                  'script_sha256': digest(__file__),
                  'validation': 'Train on one corpus and evaluate directly on a different corpus. Exclude source audio matching any known target artist, title or audio hash before fitting. All preprocessing, regularization and learner selection use source data only, with three grouped source folds.',
                  'limitations': ['Native valence/arousal scales are linearly normalized but retain different listener instructions, excerpt boundaries and populations.',
                                 'These are transfer stress tests, not proof that native targets are interchangeable.',
                                 'Missing identities and recording edits leave unknown overlaps; upstream pretrained-model overlap is unresolved.',
                                 'Matched AMG/NTWICM subsets are selected by local audio availability and provide weak song-level labels.',
                                 'No developer vibe labels, silver predictions, or product database data enter fitting.']}
        for source, (rows, y, groups, features, provenance) in corpora.items():
            for target, (test_rows, test_y, _, test_features, test_provenance) in corpora.items():
                if source == target:
                    continue
                case = source + '__to__' + target
                retained = exclude_overlap(rows, test_rows)
                training_rows = [row for row, keep in zip(rows, retained) if keep]
                item = {'training_rows': len(training_rows), 'test_rows': len(test_rows),
                        'excluded_source_ids': [row['id'] for row, keep in zip(rows, retained) if not keep],
                        'source_provenance': provenance, 'target_provenance': test_provenance}
                report['directions'][case] = item
                if len(training_rows) < 30 or len({row['group'] for row in training_rows}) < 3:
                    item['state'] = 'insufficient_disjoint_training_data'
                    save(OUT / 'cross-corpus.report.json', report)
                    continue
                baseline = np.broadcast_to(y[retained].mean(0), test_y.shape).copy()
                values = {'source_mean': baseline}
                item['methods'] = {'source_mean': metrics(test_y, baseline, TARGETS)}
                training_groups = np.array(groups)[retained].tolist()
                for family in ('attributes', 'embedding'):
                    x = features[family][retained]
                    if family == 'attributes':
                        alpha = choose_alpha(x, y[retained], training_groups, [0, 1])
                        fitted = {'linear': fit_path(x, y[retained], [alpha])[0]}
                        selected = {'linear': str(alpha)}
                    else:
                        selected = select(x, y[retained], training_groups, False)
                        all_models = candidates(x, y[retained])
                        fitted = {method: all_models[selected[method]] for method in ('linear', 'rbf', 'selected_learner')}
                    for method, model in fitted.items():
                        name = family + ':' + method
                        fitted_values = predict(model, test_features[family]) if family == 'attributes' else prediction(model, test_features[family])
                        values[name] = fitted_values
                        improvement = bootstrap_improvement(test_y, baseline, fitted_values,
                                                            [row['group'] for row in test_rows], [0, 1])
                        improvement['method'] = 'Paired target-group bootstrap of fixed zero-shot predictions; source fit and selection are not repeated.'
                        item['methods'][name] = {**metrics(test_y, fitted_values, TARGETS),
                                                 'versus_source_mean': improvement,
                                                 'source_selected_parameter': selected[method]}
                        save(OUT / 'models/cross-corpus' / f'{case}.{family}.{method}.json', {
                             'format': 'external-research-cross-corpus-1', 'source': source, 'target': target,
                             'target_names': TARGETS, 'family': family, 'model': serializable(model),
                             'training_ids': [row['id'] for row in training_rows],
                             'source_provenance': provenance, 'target_provenance': test_provenance})
                item['state'] = 'complete'
                save(OUT / f'{case}.cross-corpus.predictions.json', {'target_ids': [row['id'] for row in test_rows],
                     'observed': test_y.tolist(), 'target_names': TARGETS,
                     'predictions': {name: value.tolist() for name, value in values.items()}})
                save(OUT / 'cross-corpus.report.json', report)
                print(json.dumps({'cross_corpus': case, 'training': len(training_rows),
                                  'mae': {name: value['mae'] for name, value in item['methods'].items()}}), flush=True)
        return report
