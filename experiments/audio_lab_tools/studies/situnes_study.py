"""Bounded, grouped SiTunes post-mood feasibility diagnostics (#813)."""
import argparse
from collections import Counter
import csv
import hashlib
from pathlib import Path
import resource
import time
import unicodedata

import numpy as np
from threadpoolctl import threadpool_limits

from ..support import quality_round as evidence
from . import individual_protocol as protocol
from ..support.external_experiment import LAB, digest, save
from ..focus.focus_experiment import arrays, check_receipt, constant, inside, load, native_predict

DEFAULT = LAB / 'local/situnes-813-20261101/round-2'
DISCOVERY = LAB / 'local/datasets/discovery/research-20261101'
SOURCE = DISCOVERY / 'situnes'
SEED = 8131101
AXES = ('valence', 'arousal')
FAMILIES = ('pre', 'context', 'pre-context', 'pre-context-user', 'pre-context-user-music')
MUSIC = ('popularity', 'loudness', 'danceability', 'energy', 'key', 'speechiness',
         'acousticness', 'instrumentalness', 'valence', 'tempo', 'general_genre_id',
         'duration', 'F0final_sma_amean', 'F0final_sma_stddev',
         'audspec_lengthL1norm_sma_stddev', 'pcm_RMSenergy_sma_stddev',
         'pcm_fftMag_psySharpness_sma_amean', 'pcm_fftMag_psySharpness_sma_stddev',
         'pcm_zcr_sma_amean', 'pcm_zcr_sma_stddev')


def csv_rows(path):
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def number(value):
    return float(value) if value not in (None, '') else np.nan


def freeze(directory):
    tree = load(DISCOVERY / 'situnes-tree.response')
    expected = {r['path']: r['sha'] for r in tree['tree'] if r['type'] == 'blob'}
    inputs = {'tree.json': DISCOVERY / 'situnes-tree.response',
              'acquisition.json': SOURCE / 'acquisition.json',
              'intersection.json': SOURCE / 'intersection-audit.json'}
    names = ['SiTunes/README.md', 'Experiments/Psychological_satisfaction_prediction/README.md',
             'SiTunes/music_metadata/music_info_withname.csv']
    names += [f'SiTunes/Stage{s}/interactions.csv' for s in (1, 2, 3)]
    names += [f'SiTunes/Stage{s}/env.json' for s in (2, 3)]
    for name in names:
        path = SOURCE / name
        data = path.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual != expected[name]:
            raise ValueError('Pinned Git blob mismatch: ' + name)
        inputs[name] = path
    intersection = load(inputs['intersection.json'])
    references = []
    for source in sorted({r['source'] for rs in intersection['exact_artist_title_candidates'].values() for r in rs}):
        path = LAB / 'local/external-20261028' / source
        if digest(path) != intersection['inputs'][str(path.relative_to(LAB.parents[1]))]:
            raise ValueError('Candidate metadata changed: ' + source)
        inputs['candidates/' + source] = path
    evidence.freeze(directory, 813, {'seed': SEED,
        'families': FAMILIES, 'music_numeric_columns': MUSIC, 'bootstrap_draws': 1000,
        'outer_user': 'leave-one-out', 'outer_track': 5, 'inner_folds': 3,
        'maximum_seconds': 3600, 'maximum_rss_bytes': 2*1024**3,
        'maximum_model_bytes': 64*1024**2, 'no_audio_model': True},
        inputs, references=references,
        documents=[LAB.parent / 'investigations/situnes_813.py'])
    print({'plan_sha256': digest(directory / 'plan.json')}, flush=True)


def cohort(directory):
    root = directory / 'inputs/SiTunes'
    metadata = csv_rows(root / 'music_metadata/music_info_withname.csv')
    items = {str(int(float(r['i_id_c']))): r for r in metadata}
    if len(items) != len(metadata):
        raise ValueError('Duplicate metadata item IDs')
    rows, stages = [], {}
    for stage in (1, 2, 3):
        raw = csv_rows(root / f'Stage{stage}/interactions.csv')
        keys = [(r['user_id'], r['No.']) if stage == 1 else r['inter_id'] for r in raw]
        if len(set(keys)) != len(raw):
            raise ValueError('Duplicate stage interaction IDs')
        env = load(root / f'Stage{stage}/env.json') if stage > 1 else {}
        if stage > 1 and set(env) != set(keys):
            raise ValueError('Environment join mismatch')
        stages[str(stage)] = {'rows': len(raw), 'participants': len({r['user_id'] for r in raw}),
            'items': len({r['item_id'] for r in raw}),
            'missing_fields': dict(Counter(k for r in raw for k,v in r.items() if not v)),
            'missing_metadata': sum(r['item_id'] not in items for r in raw),
            'timestamp_range': [min(int(r['timestamp']) for r in raw), max(int(r['timestamp']) for r in raw)]}
        if stage == 1:
            continue
        for r in raw:
            pre = [number(r['emo_pre_'+a]) for a in AXES]
            post = [number(r['emo_post_'+a]) for a in AXES]
            if not np.isfinite(pre+post).all() or np.max(np.abs(pre+post)) > 1:
                raise ValueError('Invalid or missing paired human mood: explicit cohort revision required')
            item = items.get(r['item_id'], {})
            situation = env[r['inter_id']]
            if len(situation['weather']) != 4:
                raise ValueError('Unexpected weather schema')
            track = (normalize(item['singer']) + '::' + normalize(item['music'])) if item else 'missing:'+r['item_id']
            rows.append({'id': f'{stage}:'+r['inter_id'], 'user': r['user_id'], 'item': r['item_id'],
                'track': track, 'stage': str(stage), 'policy': str(stage)+':'+(r['rec_type'] or 'missing'),
                'time': str(situation['time']), 'weather': str(situation['weather'][0]),
                'pressure': number(situation['weather'][1]), 'temperature': number(situation['weather'][2]),
                'humidity': number(situation['weather'][3]), 'pre': pre, 'post': post,
                'music': {k:number(item.get(k)) if np.isfinite(number(item.get(k))) else None for k in MUSIC},
                'genre': item.get('general_genre') or 'missing'})
    return rows, stages, items


def schema(rows, family):
    numeric, categorical = [], []
    if family.startswith('pre'):
        numeric += ['pre-valence', 'pre-arousal']
    if 'context' in family:
        numeric += ['pressure', 'temperature', 'humidity']
        categorical += ['stage', 'policy', 'time', 'weather']
    if 'user' in family:
        categorical += ['user']
    if 'music' in family:
        numeric += ['music:'+k for k in MUSIC]
        categorical += ['genre']
    return {'numeric': numeric, 'categories': {k:sorted({r[k] for r in rows}) for k in categorical}}


def matrix(rows, spec):
    result = []
    for row in rows:
        values = []
        for key in spec['numeric']:
            if key.startswith('pre-'):
                values.append((row['pre'][AXES.index(key[4:])]+1)/2)
            elif key.startswith('music:'):
                values.append(row['music'][key[6:]])
            else:
                values.append(row[key])
        values += [float(row[key] == level) for key, levels in spec['categories'].items() for level in levels]
        result.append(values)
    return np.asarray(result, dtype=float)


def fit(rows, y, family, split):
    predictions = np.empty((len(rows), 6, 2))
    inner_schemas = []
    for fold in sorted(set(split)):
        train, valid = np.flatnonzero(split != fold), np.flatnonzero(split == fold)
        training = [rows[i] for i in train]
        spec = schema(training, family)
        x = matrix(training, spec)
        heads = [constant(y[train], x.shape[1]), *protocol.learner.fit_models(x, y[train], protocol.SPECS)]
        valid_x = matrix([rows[i] for i in valid], spec)
        for index, head in enumerate(heads):
            predictions[valid, index] = native_predict(head, valid_x)
        inner_schemas.append({'fold': int(fold), 'schema': spec})
    loss = np.mean((predictions-y[:,None,:])**2, axis=0)
    selected = loss.argmin(axis=0)
    spec = schema(rows, family)
    x = matrix(rows, spec)
    heads = [constant(y, x.shape[1]), *protocol.learner.fit_models(x, y, protocol.SPECS)]
    exported = []
    for column, index in enumerate(selected):
        model = heads[index]
        exported.append(protocol.learner.serial(
            {**model, 'value': model['value'][column:column+1]} if model['kind'] == 'constant'
            else protocol.learner.column_model(model, column)))
    return {'schema': spec, 'heads': exported, 'selected': selected.tolist(),
            'inner_mse_normalized': loss.tolist(), 'inner_schemas': inner_schemas}


def replay(model, rows):
    x = matrix(rows, model['schema'])
    return np.column_stack([native_predict(head, x).ravel() for head in model['heads']])*2-1


def partitions(rows):
    users = np.array([r['user'] for r in rows])
    tracks = np.array([r['track'] for r in rows])
    result = []
    for dimension, groups, outer in [('participant', users, np.array([sorted(set(users)).index(u) for u in users])),
                                    ('track', tracks, protocol.folds(tracks, count=5, seed=SEED))]:
        for fold in sorted(set(outer)):
            train, test = np.flatnonzero(outer != fold), np.flatnonzero(outer == fold)
            inner = protocol.folds(groups[train], count=3, seed=SEED)
            result.append({'evaluation': dimension, 'fold': int(fold), 'train': train.tolist(),
                           'test': test.tolist(), 'inner': inner.tolist()})
    train = np.array([i for i,r in enumerate(rows) if r['stage'] == '2'])
    test = np.array([i for i,r in enumerate(rows) if r['stage'] == '3'])
    result.append({'evaluation': 'stage2-to-stage3', 'fold': 0, 'train': train.tolist(), 'test': test.tolist(),
                   'inner': protocol.folds(users[train], count=3, seed=SEED).tolist()})
    return result


def audio_audit(directory, rows, items):
    intersection = load(directory / 'inputs/intersection.json')
    selected = intersection['exact_artist_title_candidates']
    matched = [r for r in rows if r['item'] in selected]
    candidates = {}
    for item, matches in selected.items():
        candidates[item] = {'artist': items[item]['singer'], 'title': items[item]['music'],
                            'item_duration_ms': float(items[item]['duration']), 'sources': []}
        for match in matches:
            source = load(directory / 'inputs/candidates' / match['source'])
            record = next(r for r in source['rows'] if r['id'] == match['id'])
            candidates[item]['sources'].append({**match,
                **{k:record.get(k) for k in ('audio_sha256','recording_duration','sample_start','sample_seconds','annotation_unit')}})
    tree = load(directory / 'inputs/tree.json')['tree']
    return {'candidate_items_all_stages': len(selected), 'paired_rows': len(matched),
        'paired_users': len({r['user'] for r in matched}), 'paired_items': len({r['item'] for r in matched}),
        'paired_user_item_pairs': len({(r['user'],r['item']) for r in matched}), 'verified_recordings': 0,
        'verified_listened_intervals': 0, 'candidates': candidates,
        'repository_license_paths': [r['path'] for r in tree if 'license' in r['path'].lower()],
        'repository_audio_paths': [r['path'] for r in tree if Path(r['path']).suffix.lower() in ('.wav','.mp3','.flac','.ogg','.m4a')],
        'decision': 'Do not recover a broad catalog or fit audio on this unverified intersection.',
        'limitations': ['No original SiTunes audio, edition identifiers, audio hashes or listened interval offsets.',
            'Artist/title is a candidate identity only; descriptor duration cannot identify the recording.',
            'Existing feature windows cover other corpora and cannot prove the listened interval.',
            'No repository data license found; do not infer redistribution rights from public access.']}


def prepare(directory):
    if (directory / 'cohort.receipt.json').exists():
        raise ValueError('Preserve completed cohort')
    rows, stages, items = cohort(directory)
    pre, post = np.array([r['pre'] for r in rows]), np.array([r['post'] for r in rows])
    users, tracks = [r['user'] for r in rows], [r['track'] for r in rows]
    summary = {'stages': stages, 'paired_rows': len(rows), 'participants': len(set(users)),
        'items': len({r['item'] for r in rows}), 'track_groups': len(set(tracks)),
        'participant_counts': dict(Counter(users)), 'track_counts': dict(Counter(tracks)),
        'unique_participant_item_pairs': len({(r['user'],r['item']) for r in rows}),
        'missing_metadata_paired_rows': sum(r['item'] not in items for r in rows),
        'paired_descriptive': {axis: {'pre_mean':float(pre[:,j].mean()), 'post_mean':float(post[:,j].mean()),
            'change_mean':float((post-pre)[:,j].mean()), 'pre_post_correlation':float(np.corrcoef(pre[:,j],post[:,j])[0,1]),
            'pre_change_correlation_mechanically_coupled':float(np.corrcoef(pre[:,j],(post-pre)[:,j])[0,1]),
            'pre_range':[float(pre[:,j].min()),float(pre[:,j].max())],
            'post_range':[float(post[:,j].min()),float(post[:,j].max())]} for j,axis in enumerate(AXES)}}
    save(directory / 'cohort.json', {'rows': rows, 'summary': summary})
    save(directory / 'partitions.json', partitions(rows))
    save(directory / 'audio-audit.json', audio_audit(directory, rows, items))
    evidence.seal(directory, 'cohort', [directory / n for n in ('cohort.json','partitions.json','audio-audit.json')])
    print({k:summary[k] for k in ('paired_rows','participants','items','track_groups')}, flush=True)


def predict(directory):
    check_receipt(directory, 'cohort.receipt.json')
    if (directory / 'predictions.receipt.json').exists():
        raise ValueError('Preserve completed predictions')
    tick = time.monotonic()
    rows = load(directory / 'cohort.json')['rows']
    y = (np.array([r['post'] for r in rows])+1)/2
    plans = load(directory / 'partitions.json')
    predictions = {e+'__'+name:np.full((len(rows),2), np.nan)
        for e in sorted({p['evaluation'] for p in plans})
        for name in ('mean','unchanged','participant-mean',*FAMILIES)}
    exports = []
    for p in plans:
        if time.monotonic()-tick > 3600:
            raise ValueError('Fitting time bound exceeded')
        train, test = np.array(p['train']), np.array(p['test'])
        training, testing = [rows[i] for i in train], [rows[i] for i in test]
        prefix = p['evaluation']+'__'
        predictions[prefix+'mean'][test] = y[train].mean(axis=0)*2-1
        predictions[prefix+'unchanged'][test] = [r['pre'] for r in testing]
        means = {u:np.mean([r['post'] for r in training if r['user']==u],axis=0) for u in {r['user'] for r in training}}
        predictions[prefix+'participant-mean'][test] = [means.get(r['user'], y[train].mean(axis=0)*2-1) for r in testing]
        models = {}
        for family in FAMILIES:
            model = fit(training, y[train], family, np.array(p['inner']))
            predictions[prefix+family][test] = replay(model, testing)
            models[family] = model
        exports.append({**p, 'models': models})
        print({'evaluation':p['evaluation'],'fold':p['fold'],'train':len(train),'test':len(test)}, flush=True)
    save(directory / 'models.json', exports)
    np.savez_compressed(directory / 'predictions.npz', **predictions)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    model_bytes = (directory / 'models.json').stat().st_size
    if rss > 2*1024**3 or model_bytes > 64*1024**2:
        raise ValueError('Fitting resource bound exceeded')
    save(directory / 'resources.json', {'fit_seconds':time.monotonic()-tick,'peak_rss_bytes':rss,'model_bytes':model_bytes})
    evidence.seal(directory, 'predictions', [directory / n for n in ('models.json','predictions.npz','resources.json')])


def paired_intervals(loss, users, tracks):
    _, user = np.unique(users, return_inverse=True)
    _, track = np.unique(tracks, return_inverse=True)
    nu, nt = user.max()+1, track.max()+1
    rng = np.random.default_rng(SEED)
    draws = {k:[] for k in ('participant','track','two-way')}
    for _ in range(1000):
        a = np.bincount(rng.integers(nu,size=nu),minlength=nu)[user]
        b = np.bincount(rng.integers(nt,size=nt),minlength=nt)[track]
        for key, weights in [('participant',a),('track',b),('two-way',a*b)]:
            if weights.sum():
                draws[key].append(np.average(loss,axis=0,weights=weights))
    return {'mse_difference':loss.mean(axis=0).tolist(),
        'conditional_95_percent_intervals':{k:np.quantile(v,[.025,.975],axis=0).T.tolist() for k,v in draws.items()},
        'usable_draws':{k:len(v) for k,v in draws.items()}}


def evaluate(directory):
    check_receipt(directory, 'predictions.receipt.json')
    if (directory / 'evaluation.receipt.json').exists():
        raise ValueError('Preserve completed evaluation')
    tick = time.monotonic()
    rows = load(directory / 'cohort.json')['rows']
    y = np.array([r['post'] for r in rows])
    pred = arrays(directory / 'predictions.npz')
    report = {'axes':AXES, 'native_scale':[-1,1], 'evaluations':{}}
    comparisons = [('pre','unchanged'),('context','mean'),('pre-context','pre'),
        ('pre-context-user','pre-context'),('pre-context-user-music','pre-context-user')]
    for evaluation in ('participant','track','stage2-to-stage3'):
        keep = np.isfinite(pred[evaluation+'__pre']).all(axis=1)
        users = np.array([r['user'] for r in rows])[keep]
        tracks = np.array([r['track'] for r in rows])[keep]
        values = {name: pred[evaluation+'__'+name][keep] for name in ('mean','unchanged','participant-mean',*FAMILIES)}
        assert all(np.isfinite(v).all() for v in values.values())
        losses = {name:(v-y[keep])**2 for name,v in values.items()}
        report['evaluations'][evaluation] = {'rows':int(keep.sum()), 'participants':len(set(users)),
            'tracks':len(set(tracks)), 'metrics':{name:{'mse':losses[name].mean(axis=0).tolist(),
                'mae':np.abs(v-y[keep]).mean(axis=0).tolist(),
                'mean_participant_mse':np.mean([losses[name][users==u].mean(axis=0) for u in sorted(set(users))],axis=0).tolist()}
                for name,v in values.items()},
            'paired':{a+'__vs__'+b:paired_intervals(losses[a]-losses[b],users,tracks) for a,b in comparisons},
            'per_participant_mse':{u:{name:loss[users==u].mean(axis=0).tolist() for name,loss in losses.items()} for u in sorted(set(users))}}
    train = [r for r in rows if r['stage']=='2']
    test = [r for r in rows if r['stage']=='3']
    report['stage_transfer'] = {'seen_participants':len({r['user'] for r in train}&{r['user'] for r in test}),
        'seen_tracks':len({r['track'] for r in train}&{r['track'] for r in test}),
        'test_rows_seen_track':sum(r['track'] in {t['track'] for t in train} for r in test)}
    report.update(evaluation_seconds=time.monotonic()-tick, decision='retain-production-models; metadata feasibility only',
        limitations=['Observational, policy-selected recordings and a small repeated-participant cohort.',
            'Participant-held-out and track-held-out answer separate questions; neither holds both out.',
            'Bootstrap intervals condition on fitted heads, observed users and item distribution.',
            'Context observation timing has not been independently validated for prospective use.',
            'Change scores share initial mood by construction; correlations do not establish an intervention effect.',
            'Spotify valence is a supplied input descriptor, not human truth.',
            'No verified audio intersection or general audio-model training; no anger relief or cognition labels.'])
    save(directory / 'report.json', report)
    evidence.seal(directory,'evaluation',[directory / 'report.json'])
    print({'evaluations':list(report['evaluations']),'seconds':report['evaluation_seconds'],'decision':report['decision']},flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['freeze','prepare','predict','evaluate','verify'])
    parser.add_argument('--directory',type=Path,default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
        elif args.action == 'verify':
            print(evidence.verify(directory,required_stages=['cohort','predictions','evaluation'],optional_stages=['postrun-source']))
        else:
            evidence.validate(directory,execution=True)
            {'prepare':prepare,'predict':predict,'evaluate':evaluate}[args.action](directory)


if __name__ == '__main__':
    main()
