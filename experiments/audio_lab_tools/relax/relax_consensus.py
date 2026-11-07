"""Nested, source-native Relax experts and a bounded corpus-balanced blend."""
import itertools

import numpy as np

from ..studies import individual_protocol as protocol
from ..training.external_training import folds
from ..focus.focus_experiment import head_predictions
from ..studies.mapped_vibe_rules import apply, registry

SEED = 61730
EMO, SOUND = 'emotify-artist', 'soundtracks-all'
NAT, GEN, MEMO = 'manymusic-natural', 'manymusic-generated', 'memo2496-pilot'
SOURCES = (EMO, SOUND, NAT, GEN, MEMO)
EXPERTS = (EMO, SOUND, 'manymusic', MEMO)
NATIVE = {EMO: ['calmness', 'tension'], SOUND: ['energy', 'tension', 'tender'],
          NAT: ['valence', 'arousal'], GEN: ['valence', 'arousal'], MEMO: ['valence', 'arousal']}
DIAGNOSTIC = {EMO: ['amazement', 'solemnity', 'tenderness', 'nostalgia', 'power', 'joyful_activation', 'sadness'],
              SOUND: ['valence', 'anger', 'fear', 'happy', 'sad']}


def expression(source):
    return registry()[source if source in (EMO, SOUND) else 'pmemo']['relax']['expression']


def split(data, count=3):
    names = list(data)
    groups = np.concatenate([data[s]['groups'] for s in names])
    assigned = folds(groups, count=count, seed=SEED)
    protocol.group_guard(groups, assigned)
    result, offset = {}, 0
    for name in names:
        n = len(data[name]['groups'])
        result[name] = assigned[offset:offset + n]
        protocol.group_guard(data[name]['groups'], result[name])
        offset += n
    return result


def subset(data, mask):
    return {k: v[mask] for k, v in data.items()}


def native_fit(data):
    fitted, traces = {}, {}
    for name, d in data.items():
        assignment = folds(d['groups'], count=3, seed=SEED)
        protocol.group_guard(d['groups'], assignment)
        heads, trace = protocol.select(d['x'], d['y'], assignment)
        fitted[name] = {'heads': heads, 'target_names': NATIVE[name], 'expression': expression(name)}
        traces[name] = {**trace, 'training_ids': d['ids'].tolist(),
                        'training_groups': d['groups'].tolist(), 'inner_folds': assignment.tolist()}
    return fitted, traces


def expert_matrix(models, x, independent=False):
    if (NAT in models) != (GEN in models):
        raise ValueError('ManyMusic natural and generated must enter or leave together')
    scores = {name: apply(m['expression'], head_predictions(m['heads'], x, independent), m['target_names'])
              for name, m in models.items()}
    if NAT in scores:
        scores['manymusic'] = (scores.pop(NAT) + scores.pop(GEN)) / 2
    names = [name for name in EXPERTS if name in scores]
    if not names or names[0] != EMO:
        raise ValueError('Relax requires its Emotify calmness anchor')
    return names, np.column_stack([scores[name] for name in names])


def fixed_weights(names):
    initial = {EMO: .5, SOUND: .25, 'manymusic': .125, MEMO: .125}
    auxiliary = sum(initial[n] for n in names if n != EMO)
    return np.array([1. if len(names) == 1 else .5 if n == EMO else .5 * initial[n] / auxiliary for n in names])


def weight_grid(names):
    # Prefer the zero-auxiliary control on exact ties.
    values = [np.array(w, dtype=float) / 8
              for w in itertools.product(range(9), repeat=len(names))
              if sum(w) == 8 and w[0] >= 4]
    return np.array(sorted(values, key=lambda w: tuple(-v for v in w)))


def ranking_loss(observed, scores, groups):
    """Source-local ranking: no comparisons between incompatible reference scales."""
    left, right = np.triu_indices(len(observed), 1)
    difference = observed[left] - observed[right]
    keep = (np.abs(difference) > 1e-12) & (groups[left] != groups[right])
    left, right, difference = left[keep], right[keep], difference[keep]
    if not len(left):
        return np.full(scores.shape[1], np.log(2))
    margin = (scores[left] - scores[right]) * np.sign(difference)[:, None] / .1
    return np.average(np.logaddexp(0, -margin), axis=0, weights=np.abs(difference))


def choose_weights(data, matrices, names):
    grid = weight_grid(names)
    per_source = {s: ranking_loss(d['reference'], matrices[s] @ grid.T, d['groups']) for s, d in data.items()}
    weights = np.array([.5 if s in (NAT, GEN) else 1. for s in data])
    loss = np.average(list(per_source.values()), axis=0, weights=weights)
    chosen = int(np.argmin(loss))
    return grid[chosen], {'expert_names': names, 'grid': grid.tolist(),
                         'source_losses': {s: v.tolist() for s, v in per_source.items()},
                         'macro_loss': loss.tolist(), 'selected_index': chosen}


def fit(data):
    assignments = split(data)
    matrices, traces, names = {}, {}, None
    for fold in range(3):
        training = {s: subset(d, assignments[s] != fold) for s, d in data.items()}
        fitted, native_trace = native_fit(training)
        train_groups = set(np.concatenate([d['groups'] for d in training.values()]))
        validation = {s: d['ids'][assignments[s] == fold].tolist() for s, d in data.items()}
        for source, d in data.items():
            mask = assignments[source] == fold
            if train_groups & set(d['groups'][mask]):
                raise ValueError('Cross-source component leaks into mapping validation')
            names, prediction = expert_matrix(fitted, d['x'][mask])
            if source not in matrices:
                matrices[source] = np.empty((len(d['y']), len(names)))
            matrices[source][mask] = prediction
        traces[str(fold)] = {'native': native_trace, 'validation_ids': validation}
    weights, selection = choose_weights(data, matrices, names)
    fitted, final_trace = native_fit(data)
    model = {'format': 'relax-consensus-1', 'experts': fitted, 'expert_names': names,
             'fixed_weights': fixed_weights(names).tolist(), 'learned_weights': weights.tolist(),
             'aggregation': 'mean_embedding_over_annotated_interval_then_native_prediction'}
    return model, {'mapping_selection': selection, 'crossfit': traces, 'final_native': final_trace}


def predict(model, x, independent=False):
    names, matrix = expert_matrix(model['experts'], x, independent)
    if names != model['expert_names']:
        raise ValueError('Exported expert order differs')
    return {'anchor': matrix[:, 0], 'fixed': matrix @ np.asarray(model['fixed_weights']),
            'learned': matrix @ np.asarray(model['learned_weights'])}
