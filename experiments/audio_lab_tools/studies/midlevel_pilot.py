"""Nested human-property bottleneck pilot on verified existing Soundtracks audio."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import resource
import time
import unicodedata

import numpy as np
from threadpoolctl import threadpool_limits

from . import encoder_models as models, individual_protocol as protocol
from ..support import quality_round as evidence
from .encoder_experiment import DEFAULT as ORIGINAL
from ..support.external_experiment import LAB, digest, save
from ..training.external_training import folds
from ..focus.focus_experiment import arrays, check_receipt, constant, inside, load
from .mapped_vibe_rules import apply
from .midlevel_study import DEFAULT as ACQUISITION

ROOT = LAB / 'local/midlevel-811-20261101'
DEFAULT = ROOT / 'pilot-1'
DOCUMENT = LAB / 'MIDLEVEL_PILOT_PLAN.md'
PROPERTIES = ['melody', 'articulation', 'rhythm_complexity', 'rhythm_stability', 'dissonance', 'atonality', 'mode']
VARIANTS = ('direct', 'bottleneck', 'augmented', 'oracle')
SEED = 8111101


def prepare():
    evidence.verify(ACQUISITION, required_stages=['acquisition'])
    base = ORIGINAL / 'inputs'
    meta = load(base / 'soundtracks-all.json')
    identities = {r['id']: r for r in load(base / 'audio-identities/soundtracks-all.json')['rows']}
    source = list(csv.DictReader((ACQUISITION / 'metadata/metadata.csv').open(), delimiter=';'))
    labels = [{k.strip(): v for k, v in r.items()} for r in csv.DictReader((ACQUISITION / 'metadata/annotations.csv').open())]
    labels = {r['song_id']: r for r in labels}
    index = {r['id']: i for i, r in enumerate(meta['rows'])}
    data = arrays(base / 'soundtracks-all.npz')
    rows, props, positions = [], [], []
    for row in source:
        if row['Source'] != 'soundtracks':
            continue
        key = 'soundtracks:set1:' + f"{int(row['ID at file source']):03}"
        i = index[key]
        identity = identities[key]
        if digest(identity['audio']) != identity['audio_sha256']:
            raise ValueError('Original source audio changed')
        feature = load(identity['feature_file'])
        if feature['id'] != key or feature['sample_count'] != 160000:
            raise ValueError('Wrong feature identity or interval sample count')
        np.testing.assert_array_equal(feature['prediction']['embedding']['values'], data['embedding'][i])
        expected_key = hashlib.sha256((identity['audio_sha256'] + identity['extractor_signature']
            + f"{identity['sample_start']:.6f}").encode()).hexdigest()
        if Path(identity['feature_file']).stem != expected_key:
            raise ValueError('Extractor/interval cache identity mismatch')
        for field in ('audio_sha256', 'sample_start', 'sample_seconds'):
            if meta['rows'][i][field] != identity[field]:
                raise ValueError('Source feature provenance mismatch: ' + field)
        rows.append({**meta['rows'][i], 'audio': identity['audio'],
            'midlevel_id': row['song id'], 'film': row['Album name'].strip(),
            'legacy_component': meta['groups'][i], 'feature_file': identity['feature_file'],
            'feature_sha256': digest(identity['feature_file']),
            'extractor_signature': identity['extractor_signature'],
            'recording_duration': identity['recording_duration']})
        props.append([float(labels[row['song id']][p]) for p in PROPERTIES])
        positions.append(i)
    parents, seen = list(range(len(rows))), {}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, row in enumerate(rows):
        film = re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', row['film']).casefold())
        keys = [('legacy', row['legacy_component']), ('audio', row['audio_sha256'])]
        if film:
            keys.append(('film', film))
        for key in keys:
            if key in seen:
                a, b = root(i), root(seen[key])
                parents[max(a, b)] = min(a, b)
            seen[key] = i
    groups = np.array([root(i) for i in range(len(rows))])
    assert len(rows) == 360 and len(set(groups)) >= 12
    props = np.asarray(props)
    if not np.isfinite(props).all() or props.min() < 1 or props.max() > 10:
        raise ValueError('Unexpected native property scale')
    ids = np.array(positions)
    outer = folds(groups, count=5, seed=SEED)
    protocol.group_guard(groups, outer)
    audit = {'rows': rows, 'components': len(set(groups)), 'source_counts':
        {s: sum(r['Source'] == s for r in source) for s in sorted({r['Source'] for r in source})},
        'properties': PROPERTIES, 'emotion_targets': meta['target_names'], 'property_native_scale': [1, 10],
        'property_ranges': {p: [float(props[:, j].min()), float(props[:, j].max())] for j, p in enumerate(PROPERTIES)},
        'terms': 'Metadata README provides source links and annotations but no explicit annotation license; original Soundtracks release is for academic research. No redistribution permission inferred.',
        'alignment': 'Explicit source IDs identify original Set1 excerpts. Features summarize their known center-ten-second subinterval; labels describe the entire supplied excerpt.',
        'new_memo_used': 0, 'new_audio_bytes': 0}
    save(ROOT / 'pilot-audit.json', audit)
    np.savez_compressed(ROOT / 'pilot-inputs.npz', x=data['embedding'][ids],
        y=data['y'][ids], properties=(props-1)/9, groups=groups, outer=outer)
    print(json.dumps({'rows': len(rows), 'components': audit['components'], 'source_counts': audit['source_counts']}))


def freeze(directory):
    audit = load(ROOT / 'pilot-audit.json')
    evidence.freeze(directory, 811, {'document_sha256': digest(DOCUMENT), 'seed': SEED,
        'properties': PROPERTIES, 'variants': VARIANTS, 'rows': 360, 'components': audit['components'],
        'budget_seconds': 3600, 'rss_bytes': 4 * 1024**3, 'model_bytes': 64 * 1024**2},
        {'audit.json': ROOT / 'pilot-audit.json', 'data.npz': ROOT / 'pilot-inputs.npz'},
        references=[ACQUISITION / 'acquisition.receipt.json', ACQUISITION / 'metadata_annotations.zip',
            ORIGINAL / 'plan.json', ORIGINAL / 'inputs/soundtracks-all.json', ORIGINAL / 'inputs/soundtracks-all.npz',
            *[Path(r['feature_file']) for r in audit['rows']], *[Path(r['audio']) for r in audit['rows']]],
        documents=[DOCUMENT, LAB.parent / 'investigations/midlevel_811.py'])
    print(json.dumps({'plan_sha256': digest(directory / 'plan.json')}))


def predict(heads, x):
    return np.column_stack([protocol.native_predict(head, x).ravel() for head in heads])


def fit(directory):
    if (directory / 'predictions.receipt.json').exists():
        raise ValueError('Preserve completed predictions')
    started = time.monotonic()
    data = arrays(directory / 'inputs/data.npz')
    x, y, props, groups, outer = (data[k] for k in ('x', 'y', 'properties', 'groups', 'outer'))
    predictions = {v: np.empty_like(y) for v in VARIANTS}
    prop_prediction, prop_mean = np.empty_like(props), np.empty_like(props)
    traces, outputs = {}, []

    def auxiliary(train, test, name, protected):
        assert not set(groups[train]).intersection(protected)
        split = folds(groups[train], count=3, seed=SEED)
        heads, detail = protocol.select(x[train], props[train], split)
        traces[name] = {'train': train.tolist(), 'test': test.tolist(),
            'training_components': groups[train].tolist(), 'protected_components': sorted(map(int, protected)),
            'inner_folds': split.tolist(), **detail}
        return predict(heads, x[test]), heads

    def representations(train, test, name, protected):
        inferred, heads = auxiliary(train, test, name + '/full', protected)
        cross = np.empty((len(train), len(PROPERTIES)))
        inner = folds(groups[train], count=3, seed=SEED)
        for fold in range(3):
            a, b = train[inner != fold], train[inner == fold]
            cross[inner == fold], _ = auxiliary(a, b, name + '/cross-' + str(fold),
                set(protected) | set(groups[b]))
        return {'direct': (x[train], x[test]), 'bottleneck': (cross, inferred),
            'augmented': (np.column_stack([x[train], cross]), np.column_stack([x[test], inferred])),
            'oracle': (props[train], props[test])}, heads, inferred

    for fold in range(5):
        assert time.monotonic() - started < 3600
        train, test = np.flatnonzero(outer != fold), np.flatnonzero(outer == fold)
        protected = set(groups[test])
        inner = folds(groups[train], count=3, seed=SEED)
        inner_prediction = {v: np.empty((len(train), 6, y.shape[1])) for v in VARIANTS}
        for k in range(3):
            a, b = train[inner != k], train[inner == k]
            reps, _, _ = representations(a, b, f'outer-{fold}/inner-{k}', protected | set(groups[b]))
            for variant, (xx, vv) in reps.items():
                fitted = [constant(y[a], xx.shape[1]), *protocol.learner.fit_models(xx, y[a], protocol.SPECS)]
                for j, model in enumerate(fitted):
                    inner_prediction[variant][inner == k, j] = protocol.native_predict(model, vv)
        reps, property_heads, inferred = representations(train, test, f'outer-{fold}/final', protected)
        prop_prediction[test], prop_mean[test] = inferred, props[train].mean(axis=0)
        for variant, (xx, vv) in reps.items():
            loss = np.mean((inner_prediction[variant] - y[train, None, :])**2, axis=0)
            selected = loss.argmin(axis=0)
            fitted = [constant(y[train], xx.shape[1]), *protocol.learner.fit_models(xx, y[train], protocol.SPECS)]
            heads = []
            for column, index in enumerate(selected):
                model = fitted[index]
                head = {**model, 'value': model['value'][column:column+1]} if model['kind'] == 'constant' else protocol.learner.column_model(model, column)
                heads.append(protocol.learner.serial(head))
            predictions[variant][test] = predict(heads, vv)
            path = directory / f'models/{variant}-{fold}.json'
            save(path, {'heads': heads, 'auxiliary': property_heads, 'selected': selected.tolist(),
                        'inner_mse': loss.tolist(), 'training_rows': train.tolist(), 'test_rows': test.tolist()})
            outputs.append(path)
        print(json.dumps({'midlevel_outer_fit': fold}), flush=True)
    np.savez_compressed(directory / 'predictions.npz', **predictions,
                        property_prediction=prop_prediction, property_mean=prop_mean)
    resources = {'seconds': time.monotonic() - started,
        'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        'model_bytes': sum(p.stat().st_size for p in outputs)}
    assert resources['peak_rss_bytes'] <= 4 * 1024**3 and resources['model_bytes'] <= 64 * 1024**2
    save(directory / 'fit-trace.json', {'contexts': traces, 'resources': resources})
    evidence.seal(directory, 'predictions', [directory / 'predictions.npz', directory / 'fit-trace.json', *outputs])


def evaluate(directory):
    check_receipt(directory, 'predictions.receipt.json')
    tick = time.monotonic()
    data, pred = arrays(directory / 'inputs/data.npz'), arrays(directory / 'predictions.npz')
    audit = load(directory / 'inputs/audit.json')
    outer, groups = data['outer'], data['groups']
    report = {'properties': {}, 'emotion': {}, 'vibes': {}}
    for name, truth, columns, variants, comparisons in (
        ('properties', data['properties'], PROPERTIES,
         {'predicted': pred['property_prediction'], 'training_mean': pred['property_mean']}, [('predicted', 'training_mean')]),
        ('emotion', data['y'], audit['emotion_targets'], {v: pred[v] for v in VARIANTS},
         [(v, 'direct') for v in VARIANTS if v != 'direct'])):
        for j, label in enumerate(columns):
            scores = {v: p[:, j] for v, p in variants.items()}
            report[name][label] = {'errors': {v: {'mse': float(np.mean((p-truth[:, j])**2)),
                'mae': float(np.mean(np.abs(p-truth[:, j])))} for v, p in scores.items()},
                'paired': protocol.paired(truth[:, j], scores, outer, groups, comparisons, 1000)}
    for vibe, expression in models.expressions(models.SOUND).items():
        y = apply(expression, data['y'], audit['emotion_targets'])
        scores = {v: apply(expression, pred[v], audit['emotion_targets']) for v in VARIANTS}
        paired = protocol.paired(y, scores, outer, groups, [(v, 'direct') for v in VARIANTS if v != 'direct'], 1000)
        decisions = {}
        for variant in ('bottleneck', 'augmented'):
            c = paired[variant + '__vs__direct']
            interval = c['ndcg']['conditional_95_percent_interval']
            passed = (interval is not None and interval[0] > 0 and c['ndcg']['difference'] >= .02
                      and c['top_lift']['difference'] >= .01 and c['mse']['difference'] <= .005)
            decisions[variant] = 'development-candidate' if passed else 'retain'
        report['vibes'][vibe] = {'metrics': {v: protocol.metrics(y, p, outer) for v, p in scores.items()},
                                'paired': paired, 'decision': decisions}
    report.update(resources={'fit': load(directory / 'fit-trace.json')['resources'],
        'evaluation_seconds': time.monotonic() - tick}, rows=len(groups), components=len(set(groups)),
        limitations=['Previously inspected one-domain development cohort, not independent transfer.',
            'Oracle uses true test properties and cannot be deployed.', 'Film groups do not establish composer independence.',
            'Properties/terms from publisher; atonality column direction not inferred.',
            'No new Memo, audio archive, browser integration or subjective benefit evidence.'])
    assert report['resources']['evaluation_seconds'] + report['resources']['fit']['seconds'] <= 3600
    save(directory / 'report.json', report)
    evidence.seal(directory, 'evaluation', [directory / 'report.json'])
    print(json.dumps({v: r['decision'] for v, r in report['vibes'].items()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'freeze', 'fit', 'evaluate', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'prepare': prepare()
        elif args.action == 'freeze': freeze(directory)
        elif args.action == 'verify': print(json.dumps(evidence.verify(directory, required_stages=['predictions', 'evaluation'])))
        else:
            evidence.validate(directory, execution=True)
            {'fit': fit, 'evaluate': evaluate}[args.action](directory)


if __name__ == '__main__': main()
