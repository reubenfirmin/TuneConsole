"""Class-aware alternatives for the native Coimbra and 4Q mood tasks."""
import json

import numpy as np
from threadpoolctl import threadpool_limits

from .external_calibration import ALPHAS, fit as softmax_fit, probabilities as softmax_predict
from .external_experiment import OUT, digest, save, stamp
from .external_learners import candidates, load, prediction, serializable
from .external_training import SEED, folds
from .external_transfer import probabilities


def metrics(y, values):
    p = probabilities(values)
    actual, chosen = y.argmax(1), p.argmax(1)
    count = y.shape[1]
    confusion = np.zeros((count, count), dtype=int)
    np.add.at(confusion, (actual, chosen), 1)
    support = confusion.sum(1)
    recall = np.divide(confusion.diagonal(), support, out=np.zeros(count), where=support > 0)
    precision = np.divide(confusion.diagonal(), confusion.sum(0), out=np.zeros(count), where=confusion.sum(0) > 0)
    f1 = np.divide(2 * recall * precision, recall + precision, out=np.zeros(count), where=recall + precision > 0)
    return {'accuracy': float((actual == chosen).mean()), 'balanced_accuracy': float(recall[support > 0].mean()),
            'macro_f1': float(f1.mean()), 'log_loss': float(-np.log(np.maximum(p[np.arange(len(y)), actual], 1e-12)).mean()),
            'brier_score': float(((p - y) ** 2).sum(1).mean()), 'confusion': confusion.tolist()}


def fitted_candidates(x, y):
    result = candidates(x, y)
    for alpha in ALPHAS:
        result[f'softmax:{alpha}'] = softmax_fit(x, y, alpha)
    return result


def predict(model, x):
    return softmax_predict(model, x) if model.get('kind') == 'softmax' else probabilities(prediction(model, x))


def choose(x, y, groups):
    inner = folds(groups, count=3, seed=SEED + 1)
    losses = {}
    for fold in range(3):
        train, valid = inner != fold, inner == fold
        for key, model in fitted_candidates(x[train], y[train]).items():
            loss = metrics(y[valid], predict(model, x[valid]))['log_loss'] * sum(valid)
            losses[key] = losses.get(key, 0.) + loss
    result = {family: min((key for key in losses if key.startswith(family + ':')), key=losses.get)
              for family in ('linear', 'rbf', 'neighbours', 'softmax')}
    result['selected_learner'] = min(losses, key=losses.get)
    return result


def evaluate(dataset, feature_families=None):
    with threadpool_limits(limits=1):
        rows, y, names, groups, features, provenance = load(dataset, ())
        if feature_families is not None:
            features = {name: features[name] for name in feature_families}
        if not np.isin(y, [0, 1]).all() or not (y.sum(1) == 1).all():
            raise ValueError('Classification requires one native class per recording')
        split = folds(groups)
        baseline = np.zeros_like(y)
        for fold in range(5):
            baseline[split == fold] = y[split != fold].mean(0)
        report = {'created_at': stamp(), 'dataset': dataset, 'rows': len(rows),
                  'groups': len(set(groups)), 'target_names': names, **provenance,
                  'feature_families': list(features),
                  'baseline': metrics(y, baseline), 'features': {}, 'script_sha256': digest(__file__),
                  'validation': 'Five grouped outer folds; three inner folds select parameters and learner using native multiclass log loss. All methods use identical outer folds.',
                  'limitations': ['Tag-derived mood classes do not directly label the six product vibes.',
                                 'Previously extracted pretrained embeddings can have unknown upstream overlap.']}
        saved = {}
        for family, x in features.items():
            values = {key: np.zeros_like(y) for key in ('linear', 'rbf', 'neighbours', 'softmax', 'selected_learner')}
            choices = []
            for fold in range(5):
                train, valid = split != fold, split == fold
                selected = choose(x[train], y[train], np.array(groups)[train].tolist())
                fitted = fitted_candidates(x[train], y[train])
                for key, selection in selected.items():
                    values[key][valid] = predict(fitted[selection], x[valid])
                choices.append({'fold': fold, 'chosen': selected})
            report['features'][family] = {'methods': {key: metrics(y, value) for key, value in values.items()},
                                          'choices_by_fold': choices}
            saved[family] = {key: value.tolist() for key, value in values.items()}
            selected = choose(x, y, groups)
            fitted = fitted_candidates(x, y)
            for key, selection in selected.items():
                save(OUT / 'models/classification' / f'{dataset}.{family}.{key}.json', {
                     'format': 'external-research-classification-1', 'dataset': dataset, 'family': family,
                     'target_names': names, 'model': serializable(fitted[selection]),
                     'training_ids': [row['id'] for row in rows], 'parameter_key': selection,
                     'provenance': provenance})
            print(json.dumps({'classification': dataset, 'family': family,
                              'methods': report['features'][family]['methods']}), flush=True)
            save(OUT / f'{dataset}.classification.report.json', report)
        save(OUT / f'{dataset}.classification.oof.json', {'ids': [row['id'] for row in rows],
             'folds': split.tolist(), 'observed': y.tolist(), 'predictions': saved})
        return report
