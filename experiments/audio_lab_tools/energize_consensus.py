"""Frozen activation, power/joy consensus and exercise mapping for #616."""
import numpy as np

from . import individual_protocol as protocol
from .external_training import folds
from .focus_experiment import head_predictions
from .uplift_consensus import native_average, subset

SEED = 61630
DEAM, PMEMO, SOUND, EMO = 'deam', 'pmemo', 'soundtracks-all', 'emotify-artist'
NAT, GEN, MEMO, CAL = 'manymusic-natural', 'manymusic-generated', 'memo2496-pilot', 'cal500-raw-activity'
SOURCES = (DEAM, PMEMO, SOUND, EMO, NAT, GEN, MEMO, CAL)
NATIVE = {s: ['arousal'] for s in (DEAM, PMEMO, NAT, GEN, MEMO)}
NATIVE.update({SOUND: ['energy'], EMO: ['power', 'joyful_activation'], CAL: ['Usage-Exercising']})
METHODS = ('activation', 'consensus', 'activity')
ACTIVITY_COLUMNS = {'activity': [0, 1, 2, 3], 'activity_activation_only': [0],
                    'activity_no_power': [0, 1, 3], 'activity_no_joy': [0, 1, 2]}


def reference(source, values, names):
    return values[:, [names.index(k) for k in NATIVE[source]]].mean(axis=1)


def split(data, count=3):
    names = list(data)
    groups = np.concatenate([data[s]['groups'] for s in names])
    assigned = folds(groups, count=count, seed=SEED)
    protocol.group_guard(groups, assigned)
    result, offset = {}, 0
    for source in names:
        size = len(data[source]['groups'])
        result[source] = assigned[offset:offset + size]
        offset += size
    return result


def native_fit(data):
    models, traces = {}, {}
    for source, d in data.items():
        assignment = folds(d['groups'], count=3, seed=SEED)
        protocol.group_guard(d['groups'], assignment)
        heads, trace = protocol.select(d['x'], d['y'], assignment)
        models[source] = {'heads': heads, 'target_names': NATIVE[source]}
        traces[source] = {**trace, 'training_ids': d['ids'].tolist(),
                          'training_groups': d['groups'].tolist(), 'inner_folds': assignment.tolist()}
    return models, traces


def features(experts, chunks, counts, independent=False):
    if (NAT in experts) != (GEN in experts):
        raise ValueError('ManyMusic strata must enter or leave together')
    native = {s: native_average(m['heads'], chunks, counts, independent) for s, m in experts.items() if s != CAL}
    activation = [native[s][:, 0] for s in (DEAM, PMEMO, MEMO) if s in native]
    if NAT in native:
        activation.append((native[NAT][:, 0] + native[GEN][:, 0]) / 2)
    if not activation or EMO not in native:
        raise ValueError('Activation and power/joy sources required')
    activation = np.mean(activation, axis=0)
    # An omitted Soundtracks source has zero consensus weight, with its quarter
    # transferred to power/joy. Duplicating power/joy here expresses that rule.
    energy = native[SOUND][:, 0] if SOUND in native else native[EMO].mean(axis=1)
    return np.column_stack([activation, energy, native[EMO][:, 0], native[EMO][:, 1]])


def map_fit(x, y):
    return protocol.learner.serial(protocol.learner.fit_models(x, y[:, None], [{'family': 'ridge', 'alpha': 100.}])[0])


def fit(data):
    maps, trace = {}, {'crossfit': {}}
    if CAL in data:
        assignments = split(data)
        cross = np.empty((len(data[CAL]['y']), 4))
        for fold in range(3):
            training = {s: subset(d, assignments[s] != fold) for s, d in data.items() if s != CAL}
            experts, native_trace = native_fit(training)
            mask = assignments[CAL] == fold
            valid = set(data[CAL]['groups'][mask])
            if any(valid & set(d['groups']) for d in training.values()):
                raise ValueError('Exercise mapping validation component leaked into native fit')
            cross[mask] = features(experts, data[CAL]['chunks'][mask], data[CAL]['counts'][mask])
            trace['crossfit'][str(fold)] = {'native': native_trace,
                                          'validation_ids': data[CAL]['ids'][mask].tolist()}
        maps = {name: {'columns': columns, 'head': map_fit(cross[:, columns], data[CAL]['reference'])}
                for name, columns in ACTIVITY_COLUMNS.items()}
        trace['activity'] = {'alpha': 100., 'selection': 'fixed before fitting; no target-driven search',
                             'training_ids': data[CAL]['ids'].tolist(), 'training_groups': data[CAL]['groups'].tolist()}
    experts, trace['final_native'] = native_fit(data)
    return {'format': 'energize-consensus-1', 'experts': experts, 'activity_maps': maps,
            'feature_names': ['source_balanced_arousal', 'soundtracks_energy', 'emotify_power', 'emotify_joy'],
            'fixed_weights': [.5, .25, .125, .125],
            'aggregation': 'mean_clipped_native_then_fixed_or_activity_map',
            'training_aggregation': 'mean_embedding_per_annotated_recording'}, trace


def predict(model, chunks, counts, independent=False):
    x = features(model['experts'], chunks, counts, independent)
    result = {'activation': x[:, 0], 'consensus': x @ np.array(model['fixed_weights']),
              'consensus_no_power': x @ np.array([.5, .25, 0., .25]),
              'consensus_no_joy': x @ np.array([.5, .25, .25, 0.])}
    for name, mapping in model['activity_maps'].items():
        result[name] = head_predictions([mapping['head']], x[:, mapping['columns']], independent)[:, 0]
    if CAL in model['experts']:
        result['direct_activity'] = native_average(model['experts'][CAL]['heads'], chunks, counts, independent)[:, 0]
    return result
