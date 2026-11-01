"""Source-native Uplift experts with fixed and nested learned consensus."""
import itertools

import numpy as np

from . import individual_protocol as protocol
from .external_training import folds
from .focus_experiment import head_predictions
from .mapped_vibe_rules import apply, registry

SEED = 61830
DEAM, PMEMO, EMO, SOUND = 'deam', 'pmemo', 'emotify-artist', 'soundtracks-all'
JAM, NAT, GEN, MEMO = 'jamendo-human-4shards', 'manymusic-natural', 'manymusic-generated', 'memo2496-pilot'
SOURCES = (DEAM, PMEMO, SOUND, JAM, EMO, NAT, GEN, MEMO)
EXPERTS = (DEAM, PMEMO, SOUND, JAM, EMO, 'manymusic', MEMO)
VALENCE = (DEAM, PMEMO, 'manymusic', MEMO)
NATIVE = {s: ['valence'] for s in (DEAM, PMEMO, NAT, GEN, MEMO)}
NATIVE.update({SOUND: ['valence', 'happy'], JAM: ['happy'], EMO: ['joyful_activation']})
METHODS = ('anchor', 'fixed', 'learned')


def expression(source):
    return registry()[source if source in (DEAM, PMEMO, EMO, SOUND, JAM) else PMEMO]['uplift']['expression']


def split(data, count=3):
    names = list(data)
    groups = np.concatenate([data[s]['groups'] for s in names])
    assigned = folds(groups, count=count, seed=SEED)
    protocol.group_guard(groups, assigned)
    result, offset = {}, 0
    for name in names:
        n = len(data[name]['groups'])
        result[name] = assigned[offset:offset + n]
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


def native_average(heads, chunks, counts, independent=False):
    n, width, _ = chunks.shape
    counts = np.asarray(counts)
    if (counts.shape != (n,) or not np.issubdtype(counts.dtype, np.integer)
            or np.any(counts < 1) or np.any(counts > width)):
        raise ValueError('Invalid excerpt counts')
    valid = np.arange(width)[None, :] < counts[:, None]
    prediction = head_predictions(heads, chunks[valid], independent)
    result = np.zeros((n, len(heads)))
    np.add.at(result, np.repeat(np.arange(n), counts), prediction)
    return result / counts[:, None]


def expert_matrix(models, chunks, counts, independent=False):
    if (NAT in models) != (GEN in models):
        raise ValueError('ManyMusic strata must enter or leave together')
    scores = {s: apply(m['expression'], native_average(m['heads'], chunks, counts, independent), m['target_names'])
              for s, m in models.items()}
    if NAT in scores:
        scores['manymusic'] = (scores.pop(NAT) + scores.pop(GEN)) / 2
    names = [s for s in EXPERTS if s in scores]
    if not names or names[0] != DEAM:
        raise ValueError('The DEAM control must remain available')
    return names, np.column_stack([scores[s] for s in names])


def fixed_weights(names):
    valence = [s for s in names if s in VALENCE]
    emotion = {s: w for s, w in ((SOUND, .25), (JAM, .125), (EMO, .125)) if s in names}
    if not valence or not emotion:
        raise ValueError('Consensus requires valence and emotion supervision')
    total = sum(emotion.values())
    return np.array([.5 / len(valence) if s in valence else .5 * emotion[s] / total for s in names])


def weight_grid(names):
    candidates = []
    for integers in itertools.product(range(5), repeat=len(names)):
        if sum(integers) != 4 or sum(v for s, v in zip(names, integers) if s in VALENCE) < 2:
            continue
        if EMO in names and integers[names.index(EMO)] > 1:
            continue
        candidates.append(tuple(v / 4 for v in integers))
    candidates.append(tuple(fixed_weights(names)))
    return np.array(sorted(set(candidates), key=lambda w: tuple(-v for v in w)))


def ranking_loss(observed, scores, groups):
    left, right = np.triu_indices(len(observed), 1)
    delta = observed[left] - observed[right]
    keep = (np.abs(delta) > 1e-12) & (groups[left] != groups[right])
    left, right, delta = left[keep], right[keep], delta[keep]
    if not len(left):
        return np.full(scores.shape[1], np.log(2))
    total = np.zeros(scores.shape[1])
    for start in range(0, len(left), 4096):
        block = slice(start, start + 4096)
        margin = (scores[left[block]] - scores[right[block]]) * np.sign(delta[block])[:, None] / .1
        total += (np.logaddexp(0, -margin) * np.abs(delta[block])[:, None]).sum(axis=0)
    return total / np.abs(delta).sum()


def choose_weights(data, matrices, names):
    grid = weight_grid(names)
    losses = {s: ranking_loss(d['reference'], matrices[s] @ grid.T, d['groups']) for s, d in data.items()}
    weights = [.5 if s in (NAT, GEN) else 1. for s in data]
    loss = np.average(list(losses.values()), axis=0, weights=weights)
    chosen = int(np.argmin(loss))
    return grid[chosen], {'expert_names': names, 'grid': grid.tolist(),
                         'source_losses': {s: v.tolist() for s, v in losses.items()},
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
                raise ValueError('Component leaked into source-weight validation')
            names, predicted = expert_matrix(fitted, d['chunks'][mask], d['counts'][mask])
            if source not in matrices:
                matrices[source] = np.empty((len(d['y']), len(names)))
            matrices[source][mask] = predicted
        traces[str(fold)] = {'native': native_trace, 'validation_ids': validation}
    weights, selection = choose_weights(data, matrices, names)
    fitted, final = native_fit(data)
    return {'format': 'uplift-consensus-1', 'experts': fitted, 'expert_names': names,
            'fixed_weights': fixed_weights(names).tolist(), 'learned_weights': weights.tolist(),
            'aggregation': 'mean_clipped_native_then_fixed_map',
            'training_aggregation': 'mean_embedding_per_annotated_recording'}, {
                'mapping_selection': selection, 'crossfit': traces, 'final_native': final}


def predict(model, chunks, counts, independent=False):
    names, matrix = expert_matrix(model['experts'], chunks, counts, independent)
    if names != model['expert_names']:
        raise ValueError('Exported expert order differs')
    return {'anchor': matrix[:, 0], 'fixed': matrix @ model['fixed_weights'],
            'learned': matrix @ model['learned_weights']}
