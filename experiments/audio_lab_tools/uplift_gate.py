"""Small convex arousal mixer; native Uplift target/head procedure stays fixed."""
import itertools

import numpy as np

from . import individual_protocol as protocol, uplift_consensus as native
from .external_training import folds

GRID = tuple(itertools.product((-1., 0., 1.), (-2., 0., 2.)))
PENALTY = .001
CONSTANT = {native.DEAM: .5, native.SOUND: .25, native.EMO: .25}


def gate(arousal, parameter):
    intercept, slope = parameter
    return 1 / (1 + np.exp(-(intercept + slope * (arousal - .5))))


def family_scores(names, matrix):
    weights = native.fixed_weights(names)
    valence = np.array([name in native.VALENCE for name in names])
    return (matrix[:, valence] @ (weights[valence] / weights[valence].sum()),
            matrix[:, ~valence] @ (weights[~valence] / weights[~valence].sum()))


def mix(names, matrix, arousal, parameter):
    valence, emotion = family_scores(names, matrix)
    weight = gate(arousal, parameter)
    return weight * valence + (1 - weight) * emotion


def context_fit(data):
    heads, trace = {}, {}
    for source in (native.DEAM, native.PMEMO):
        d = data[source]
        split = folds(d['groups'], count=3, seed=native.SEED)
        heads[source], selection = protocol.select(d['x'], d['arousal'][:, None], split)
        trace[source] = {**selection, 'training_ids': d['ids'].tolist(),
                         'training_groups': d['groups'].tolist(), 'inner_folds': split.tolist()}
    return heads, trace


def context_predict(heads, chunks, counts, independent=False):
    return np.mean([native.native_average(value, chunks, counts, independent)[:, 0]
                    for value in heads.values()], axis=0)


def fit(data):
    assignment = native.split(data)
    matrices, contexts, traces = {}, {}, {}
    for fold in range(3):
        training = {s: native.subset(d, assignment[s] != fold) for s, d in data.items()}
        experts, native_trace = native.native_fit(training)
        heads, context_trace = context_fit(training)
        fit_groups = set(np.concatenate([d['groups'] for d in training.values()]))
        validation = {}
        for source, d in data.items():
            keep = assignment[source] == fold
            if fit_groups & set(d['groups'][keep]):
                raise ValueError('Head training includes mixer-validation component')
            names, matrix = native.expert_matrix(experts, d['chunks'][keep], d['counts'][keep])
            if source not in matrices:
                matrices[source] = np.empty((len(d['y']), len(names)))
                contexts[source] = np.empty(len(d['y']))
            matrices[source][keep] = matrix
            contexts[source][keep] = context_predict(heads, d['chunks'][keep], d['counts'][keep])
            validation[source] = {'ids': d['ids'][keep].tolist(), 'groups': d['groups'][keep].tolist()}
        traces[str(fold)] = {'native': native_trace, 'arousal': context_trace, 'validation': validation}
    losses = {s: native.ranking_loss(d['reference'], np.column_stack([
        mix(names, matrices[s], contexts[s], p) for p in GRID]), d['groups']) for s, d in data.items()}
    macro = np.average(list(losses.values()), axis=0,
                       weights=[.5 if s in (native.NAT, native.GEN) else 1 for s in data])
    penalized = macro + PENALTY * np.sum(np.square(GRID), axis=1)
    chosen = int(np.argmin(penalized))
    experts, final_native = native.native_fit(data)
    heads, final_context = context_fit(data)
    return {'format': 'uplift-arousal-gate-1', 'experts': experts, 'context': heads,
            'parameter': GRID[chosen], 'expert_names': names}, {
                'crossfit': traces, 'final_native': final_native, 'final_context': final_context,
                'selection': {'grid': GRID, 'penalty': PENALTY, 'selected_index': chosen,
                              'source_loss': {s: v.tolist() for s, v in losses.items()},
                              'macro_loss': macro.tolist(), 'penalized_loss': penalized.tolist()}}


def predict(model, chunks, counts, independent=False):
    names, matrix = native.expert_matrix(model['experts'], chunks, counts, independent)
    arousal = context_predict(model['context'], chunks, counts, independent)
    constant = np.array([CONSTANT.get(s, 0.) for s in names])
    constant /= constant.sum()
    return {'gate': mix(names, matrix, arousal, model['parameter']),
            'fixed': matrix @ native.fixed_weights(names), 'constant': matrix @ constant,
            'anchor': matrix[:, names.index(native.DEAM)], 'predicted_arousal': arousal}
