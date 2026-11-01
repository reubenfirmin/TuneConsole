"""Nested native-attribute and audio/attribute Focus procedures for #615."""
import numpy as np

from . import individual_protocol as protocol
from .external_training import folds
from .focus_experiment import constant, head_predictions, native_predict, select_heads
from .uplift_consensus import native_average, subset

SEED = 61530
CAL = 'cal500-raw-activity'
VA = ('deam', 'pmemo', 'manymusic-natural', 'manymusic-generated', 'memo2496-pilot')
SOUND, EMO = 'soundtracks-all', 'emotify-artist'
SOURCES = (CAL, *VA, SOUND, EMO)
TARGETS = ['Usage-Reading', 'Usage-Studying']
ATTRIBUTES = ['Song-High_Energy', 'Song-Changing_Energy_Level', 'Song-Heavy_Beat', 'Song-Fast_Tempo',
              'Emotion-Calming_/_Soothing', 'Emotion-Arousing_/_Awakening', 'Emotion-Pleasant_/_Comfortable',
              'Emotion-Tender_/_Soft', 'Emotion-Powerful_/_Strong', 'Emotion-Laid-back_/_Mellow']
NATIVE = {s: ['arousal', 'valence'] for s in VA}
NATIVE.update({CAL: ATTRIBUTES, SOUND: ['energy', 'tension', 'tender'], EMO: ['calmness', 'tension', 'power']})
FEATURES = [*ATTRIBUTES, 'consensus_arousal', 'consensus_valence', 'soundtracks_energy',
            'soundtracks_tension', 'soundtracks_tender', 'emotify_calmness', 'emotify_tension', 'emotify_power']
METHODS = ('direct', 'attributes', 'hybrid')
MAPS = ('attributes', 'hybrid', 'cal_only', 'external_only')


def split(groups):
    result = folds(groups, count=3, seed=SEED)
    protocol.group_guard(groups, result)
    return result


def protect(data, groups):
    forbidden = set(groups)
    return {s: subset(d, np.array([g not in forbidden for g in d['groups']])) for s, d in data.items()}


def native_fit(data):
    experts, trace = {}, {}
    for source, d in data.items():
        y = d['semantic'] if source == CAL else d['y']
        inner = split(d['groups'])
        heads, selection = protocol.select(d['x'], y, inner)
        experts[source] = {'heads': heads, 'target_names': NATIVE[source]}
        trace[source] = {**selection, 'training_ids': d['ids'].tolist(),
                         'training_groups': d['groups'].tolist(), 'inner_folds': inner.tolist()}
    return experts, trace


def attribute_features(experts, chunks, counts, independent=False):
    native = {s: native_average(e['heads'], chunks, counts, independent) for s, e in experts.items()}
    families = [native['deam'], native['pmemo'], native['memo2496-pilot'],
                (native['manymusic-natural'] + native['manymusic-generated']) / 2]
    consensus = np.mean(families, axis=0)
    return np.column_stack([native[CAL], consensus, native[SOUND], native[EMO]])


def map_inputs(name, attributes, embeddings):
    if name == 'hybrid':
        return np.column_stack([embeddings, attributes])
    if name == 'cal_only':
        return attributes[:, :len(ATTRIBUTES)]
    if name == 'external_only':
        return attributes[:, len(ATTRIBUTES):]
    if name == 'attributes':
        return attributes
    raise ValueError('Unknown feature map')


def crossfit(data):
    d = data[CAL]
    assignment = split(d['groups'])
    predicted = np.empty((len(d['y']), len(FEATURES)))
    trace = {}
    for fold in range(3):
        valid = assignment == fold
        training = protect(data, d['groups'][valid])
        experts, native_trace = native_fit(training)
        predicted[valid] = attribute_features(experts, d['chunks'][valid], d['counts'][valid])
        trace[str(fold)] = {'experts': experts, 'native': native_trace,
                            'validation_ids': d['ids'][valid].tolist(), 'validation_groups': d['groups'][valid].tolist()}
    return predicted, trace


def candidates(x, y):
    return [constant(y, x.shape[1]), *protocol.learner.fit_models(x, y, protocol.SPECS)]


def selected_heads(models, selected):
    result = []
    for column, index in enumerate(selected):
        model = models[index]
        result.append(protocol.learner.serial({**model, 'value': model['value'][column:column + 1]}
            if model['kind'] == 'constant' else protocol.learner.column_model(model, column)))
    return result


def fit(data):
    d = data[CAL]
    inner = split(d['groups'])
    pooled = {m: np.empty((len(d['y']), 1 + len(protocol.SPECS), 2)) for m in MAPS}
    trace = {'selection': {}, 'training_groups': d['groups'].tolist(), 'training_ids': d['ids'].tolist()}
    for fold in range(3):
        valid = inner == fold
        training = protect(data, d['groups'][valid])
        train = training[CAL]
        cross, cross_trace = crossfit(training)
        experts, native_trace = native_fit(training)
        held = attribute_features(experts, d['chunks'][valid], d['counts'][valid])
        trace['selection'][str(fold)] = {'crossfit': cross_trace, 'experts': experts, 'native': native_trace,
            'validation_ids': d['ids'][valid].tolist(), 'validation_groups': d['groups'][valid].tolist()}
        for method in MAPS:
            x = map_inputs(method, cross, train['x'])
            xx = map_inputs(method, held, d['x'][valid])
            for i, model in enumerate(candidates(x, train['y'])):
                pooled[method][valid, i] = native_predict(model, xx)
    cross, trace['final_crossfit'] = crossfit(data)
    experts, trace['final_native'] = native_fit(data)
    mappings, trace['map_selection'] = {}, {}
    for method in MAPS:
        losses = np.mean((pooled[method] - d['y'][:, None, :]) ** 2, axis=0)
        selected = losses.argmin(axis=0)
        mappings[method] = selected_heads(candidates(map_inputs(method, cross, d['x']), d['y']), selected)
        trace['map_selection'][method] = {'selected': selected.tolist(), 'inner_mse': losses.tolist(),
                                         'specs': [{'family': 'mean'}, *protocol.SPECS]}
    direct, trace['direct'] = protocol.select(d['x'], d['y'], inner)
    observed, trace['observed'] = protocol.select(d['semantic'], d['y'], inner)
    latest_historical, trace['latest_historical'] = select_heads(d['x'], d['y'], inner)
    latest_browser, trace['latest_browser'] = select_heads(d['browser'][:, 0], d['y'], inner)
    return {'format': 'focus-mapping-1', 'target_names': TARGETS, 'feature_names': FEATURES,
            'experts': experts, 'maps': mappings, 'direct': direct, 'observed': observed,
            'latest_historical': latest_historical, 'latest_browser': latest_browser,
            'training_aggregation': 'mean_embedding_per_annotated_recording',
            'aggregation': 'mean_clipped_native_then_activity_map; hybrid also uses mean raw embedding'}, trace


def predict(model, chunks, counts, independent=False):
    attrs = attribute_features(model['experts'], chunks, counts, independent)
    embeddings = np.array([x[:n].mean(axis=0) for x, n in zip(chunks, counts, strict=True)])
    result = {'direct': native_average(model['direct'], chunks, counts, independent),
              'latest_historical': native_average(model['latest_historical'], chunks, counts, independent),
              'latest_browser': native_average(model['latest_browser'], chunks, counts, independent)}
    result.update({m: head_predictions(heads, map_inputs(m, attrs, embeddings), independent)
                   for m, heads in model['maps'].items()})
    return result, attrs


def export(model, method, plan_hash, signature):
    artifact = {'format': 'focus-mapping-candidate-1', 'procedure': method,
                'target_names': TARGETS, 'source_plan_sha256': plan_hash, 'embedding_signature': signature,
                'training_aggregation': model['training_aggregation'], 'aggregation': model['aggregation']}
    if method == 'direct':
        artifact['heads'] = model['direct']
        artifact['aggregation'] = 'mean_clipped_activity_predictions'
    else:
        artifact.update(experts=model['experts'], heads=model['maps'][method], feature_names=FEATURES)
    return artifact


def exported_predict(artifact, chunks, counts, independent=False):
    if artifact['procedure'] == 'direct':
        return native_average(artifact['heads'], chunks, counts, independent)
    attrs = attribute_features(artifact['experts'], chunks, counts, independent)
    embeddings = np.array([x[:n].mean(axis=0) for x, n in zip(chunks, counts, strict=True)])
    return head_predictions(artifact['heads'], map_inputs(artifact['procedure'], attrs, embeddings), independent)
