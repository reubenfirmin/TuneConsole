"""Evaluation-only Memo confirmation, with predictions sealed before targets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import resource
from tempfile import TemporaryDirectory
import time
import zipfile

import numpy as np
from threadpoolctl import threadpool_limits

from . import individual_protocol as protocol
from ..support import quality_round as evidence
from ..datasets.dataset_range import RemoteZip
from ..support.external_experiment import DATA, LAB, digest, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, inside, load
from .mapped_vibe_inference import replay, feature_values
from .mapped_vibe_rules import registry, apply
from ..audio.memo_confirmation_audio import extract as extract_audio, provenance, WORKER
from ..evaluation.product_baseline_audit import predict as shared_predict
from ..support.quality_baselines import predict as specialist_predict, experts
from ..audio.quality_reference_audio import GRAPH, PACKAGE
from ..relax.relax_experiment import global_groups

DEFAULT = LAB / 'local/memo-confirmation-710-20261101/round-1'
PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
META = DATA / 'discovery/memo2496/metadata'
VIBES = ('cruise', 'relax', 'uplift', 'energize')
LIMIT = 1024 ** 3


def holdout_pairs(y, scores, groups, comparisons, draws=1000):
    """Resample whole components of one external holdout, without invented folds."""
    groups = np.asarray(groups)
    blocks = [np.flatnonzero(groups == g) for g in sorted(set(groups))]
    outer = np.zeros(len(y), dtype=int)
    names = sorted(scores)
    point = {k: protocol.brief_metrics(y, scores[k], outer) for k in names}
    rng = np.random.default_rng(protocol.SEED)
    samples = {k: [] for k in names}
    for _ in range(draws):
        ids = np.concatenate([blocks[i] for i in rng.integers(len(blocks), size=len(blocks))])
        for k in names:
            samples[k].append(protocol.brief_metrics(y[ids], scores[k][ids], outer[ids]))
    result = {}
    for a, b in comparisons:
        delta = np.asarray(samples[a]) - np.asarray(samples[b])
        result[a + '__vs__' + b] = {name: {'difference': float((point[a]-point[b])[i]),
            'conditional_95_percent_interval': np.quantile(delta[np.isfinite(delta[:, i]), i], [.025,.975]).tolist()
                if np.isfinite(delta[:, i]).any() else None,
            'usable_draws': int(np.isfinite(delta[:, i]).sum()), 'draws': draws}
            for i, name in enumerate(('ndcg', 'top_lift', 'mse'))}
    return result


def targets(paths, song_ids):
    """Open only requested songs after prediction sealing; blank timestamps stay missing."""
    result = {k: {} for k in song_ids}
    for label, path in paths.items():
        with path.open() as stream:
            for row in csv.DictReader(stream):
                key = row['song_id']
                if key not in result:
                    continue
                values = [row.get(f'sample_{s*1000}ms', '') for s in range(15,75)]
                if any(v is None or not v.strip() for v in values):
                    result[key][label] = {'value': None, 'missing_seconds': [s for s,v in zip(range(15,75), values) if not v or not v.strip()]}
                    continue
                v = np.asarray(values, dtype=float)
                if not np.isfinite(v).all() or np.max(np.abs(v)) > 1:
                    raise ValueError('Invalid native trajectory')
                result[key][label] = {'value': float(((v+1)/2).mean()), 'timestamps': 60}
    return result


def freeze(directory):
    reservation = evidence.reserve_memo()
    inputs = {'reservation.json': evidence.RESERVED, 'songs.csv': META / 'songs_info_all.csv',
        'valence.csv': META / 'valence_all_average.csv', 'arousal.csv': META / 'arousal_all_average.csv',
        'archive.json': DATA / 'discovery/memo2496-figshare.json',
        'named-priors.json': PREVIOUS / 'inputs/named-priors.json',
        'prior-deam.json': PREVIOUS / 'models/deam.embedding.json',
        'shared.json': LAB.parent.parent / 'src/yt_playlist/rec/data/emotify-vibes.json'}
    inputs.update({f'adopted-{v}.json': LAB.parent.parent / f'src/yt_playlist/rec/data/{v}-vibes.json' for v in VIBES})
    known = []
    for p in sorted((LAB / 'local/per-vibe-20261029/all-sources-1/inputs').glob('*.json')):
        inputs['identities/' + p.name] = p
        known.extend(load(p).get('rows', []))
    local = LAB / 'local/individual-20261030/local-1/labels.json'
    inputs['local-identities.json'] = local
    known.extend(load(local)['rows'])
    with inputs['songs.csv'].open() as stream:
        songs = {r['file_name']: {'song_id':r['song_id'], 'file_name':r['file_name'], 'platform':r['platform']}
                 for r in csv.DictReader(stream)}
    selected = [{'id':'memo2496:' + songs[r['name']]['song_id'], **songs[r['name']], 'member':r,
        'artist':'', 'title':'', 'group':'memo2496:' + songs[r['name']]['song_id']} for r in reservation['selected']]
    plan = evidence.freeze(directory, 710, {'selection':reservation['rule'],
        'maximum_recordings':128, 'requested_byte_budget':LIMIT, 'minimum_recordings':80, 'minimum_components':60,
        'interval':[15,75], 'chunks':list(range(15,75,10)), 'attribute_names':load(PREVIOUS / 'plan.json')['attribute_names'],
        'extraction':provenance(), 'bootstrap_draws':1000,
        'gain':{'ndcg':.02,'top_lift':.01,'positive_ndcg_lower_bound':True},
        'regression':{'ndcg':-.01,'top_lift':-.01,'mse':.005},
        'budget':{'acquisition_seconds':1800,'extraction_seconds':7200,'concurrent_extraction_workers':2,'per_process_rss_bytes':LIMIT},
        'prior_cruise':'Original remaining-1 DEAM native VA heads, unchanged; native clip per chunk, mean VA, fixed Cruise mapping.',
        'aggregation':'All six exact chunks, native clip then average then fixed mapping; shared control mean embedding then exact two-stage six-label normalization.',
        'outcomes':'Never fit, calibrate or select. Seal all predictions before opening selected VA trajectories.'}, inputs,
        references=[GRAPH,PACKAGE/'METADATA',PACKAGE/'RECORD'], documents=[WORKER,Path(__file__).with_name('test_memo_confirmation.py')])
    save(directory/'inputs/selection.json', {'rows':selected,'known':known,
        'exposure':'Additional filenames absent from all prior Memo pilot selections; all nonpilot Memo reserved. Artist names absent; same-corpus coverage, not artist or domain independence. Unknown aliases and encoder pretraining overlap unresolved.'})
    plan['inputs']['inputs/selection.json'] = digest(directory/'inputs/selection.json')
    save(directory/'plan.json',plan)
    print(json.dumps({'frozen':str(directory),'plan_sha256':digest(directory/'plan.json'),'selected':len(selected)}),flush=True)


def acquire(directory):
    if (directory/'acquisition.receipt.json').exists():
        check_receipt(directory,'acquisition.receipt.json'); return
    started = time.monotonic()
    selection = load(directory/'inputs/selection.json')
    item = next(r for r in load(directory/'inputs/archive.json')['files'] if r['name']=='MusicRawData.zip')
    rows = selection['rows']
    def branch(i, temporary):
        reader = RemoteZip(item['download_url'],item['size'],Path(temporary)/str(i),budget=LIMIT//4)
        acquired, rejected = [], []
        with zipfile.ZipFile(reader) as archive:
            for row in rows[i::4]:
                try:
                    if time.monotonic()-started>1800: raise ValueError('Acquisition time limit')
                    member = archive.getinfo(row['file_name'])
                    expected = row['member']
                    if (member.file_size,member.compress_size,f'{member.CRC:08x}') != (expected['bytes'],expected['compressed_bytes'],expected['crc32']):
                        raise ValueError('Published member changed')
                    data = archive.read(row['file_name'])
                    path = directory/'audio'/row['file_name']
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(data)
                    acquired.append({**row,'audio':str(path),'audio_sha256':digest(path),'bytes':len(data),'crc32_verified':True})
                    print(json.dumps({'memo_acquired':row['id'],'worker':i,'bytes':len(data)}),flush=True)
                except (OSError, ValueError, zipfile.BadZipFile) as error:
                    rejected.append({'id':row['id'],'error':str(error)})
        return acquired,rejected,{'requested_bytes':reader.requested,'received_bytes':reader.received,'ranges':reader.ranges}
    with TemporaryDirectory(prefix='ranges-',dir=directory) as temporary, ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(branch,i,temporary) for i in range(4)]
        results = [f.result() for f in pending]
    obtained = [r for a,_,_ in results for r in a]
    groups = global_groups([*obtained,*selection['known']])
    known = set(groups[len(obtained):])
    retained = [{**r,'component':int(groups[i])} for i,r in enumerate(obtained) if groups[i] not in known]
    exclusions = [r['id'] for i,r in enumerate(obtained) if groups[i] in known]
    report = {'created_at':stamp(),'rows':retained,'known_component_exclusions':exclusions,
        'failures':[r for _,e,_ in results for r in e],'workers':[r for _,_,r in results],
        'requested_bytes':sum(r['requested_bytes'] for _,_,r in results),
        'audio_bytes':sum(r['bytes'] for r in obtained),'seconds':time.monotonic()-started,
        'whole_archive_md5_verified':False, 'artist_names_available':0}
    save(directory/'acquisition.json',report)
    evidence.seal(directory,'acquisition',[directory/'acquisition.json',*[Path(r['audio']) for r in obtained]])
    print(json.dumps({'acquired':len(retained),'excluded':exclusions,'requested_bytes':report['requested_bytes'],'seconds':report['seconds']}),flush=True)


def extract(directory):
    check_receipt(directory,'acquisition.receipt.json')
    if (directory/'extraction.receipt.json').exists():
        check_receipt(directory,'extraction.receipt.json');return
    plan = load(directory/'plan.json')
    rows,rejected,resources,caches = extract_audio(directory,load(directory/'acquisition.json')['rows'],plan['protocol']['attribute_names'])
    save(directory/'features.json',{'rows':rows,'rejected':rejected,'resources':resources})
    evidence.seal(directory,'extraction',[directory/'features.json',*caches])


def predict(directory):
    check_receipt(directory,'extraction.receipt.json')
    if (directory/'predictions.receipt.json').exists():
        check_receipt(directory,'predictions.receipt.json');return
    rows = load(directory/'features.json')['rows']
    names = load(directory/'plan.json')['protocol']['attribute_names']
    priors = load(directory/'inputs/named-priors.json')
    shared = load(directory/'inputs/shared.json')
    started = time.monotonic()
    result, native = {}, {}
    for prep in ('historical','browser'):
        x = np.array([[c[prep]['embedding'] for c in r['chunks']] for r in rows]).reshape(-1,6,200)
        a = np.array([[[c[prep]['attributes'][n] for n in names] for c in r['chunks']] for r in rows],dtype=float).reshape(-1,6,27)
        common = shared_predict(shared,x.mean(1))
        for vibe in VIBES:
            key = prep+'__'+vibe
            adopted = load(directory/f'inputs/adopted-{vibe}.json')
            current, outputs = specialist_predict(adopted,x,np.full(len(x),6))
            result[key+'__current'] = current
            for source, vals in outputs.items():
                for label, value in vals.items():
                    if label in ('valence','arousal'):native[key+'__current__'+source+'__'+label] = value
            if vibe=='cruise':
                prior = load(directory/'inputs/prior-deam.json')
                yy = np.column_stack([replay(h,x.reshape(-1,200))[:,0].reshape(-1,6).mean(1) for h in prior['heads']])
                reference = registry()['pmemo'][vibe]['expression']
            else:
                prior = priors[vibe]
                data = {'embedding':x.reshape(-1,200),'attributes':a.reshape(-1,27)}
                yy = replay(prior['model'],feature_values(data,prior['family'])).reshape(len(x),6,-1).mean(1)
                reference = prior['rule']['expression']
            result[key+'__prior'] = apply(reference,yy,prior['target_names'])
            for j,label in enumerate(prior['target_names']):
                if label in ('valence','arousal'):native[key+'__prior__'+label] = yy[:,j]
            label = 'concentrate' if vibe=='focus' else vibe
            result[key+'__shared'] = common[:,shared['labels'].index(label)]
    np.savez_compressed(directory/'predictions.npz',**result,**native)
    save(directory/'prediction-trace.json',{'created_at':stamp(),'ids':[r['id'] for r in rows],
        'new_targets_opened':False,'fit_count':0,'seconds':time.monotonic()-started,
        'python_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024})
    evidence.seal(directory,'predictions',[directory/'predictions.npz',directory/'prediction-trace.json'])
    print(json.dumps({'predictions_sealed':len(rows),'new_targets_opened':False}),flush=True)


def evaluate(directory):
    check_receipt(directory,'predictions.receipt.json')
    opened = stamp()
    rows = load(directory/'features.json')['rows']
    observed = targets({n:directory/f'inputs/{n}.csv' for n in ('valence','arousal')},[r['song_id'] for r in rows])
    keep = np.array([all(observed[r['song_id']].get(n,{}).get('value') is not None for n in ('valence','arousal')) for r in rows],dtype=bool)
    y = np.array([[observed[r['song_id']][n]['value'] for n in ('valence','arousal')] for r,k in zip(rows,keep) if k])
    groups = np.array([r['component'] for r in rows])[keep]
    prediction = arrays(directory/'predictions.npz')
    tables, conclusions, native = {}, {}, {}
    supported = len(y)>=80 and len(set(groups))>=60
    for prep in ('historical','browser'):
        for vibe in VIBES:
            key = prep+'__'+vibe
            scores = {m:prediction[key+'__'+m][keep] for m in ('current','prior','shared')}
            truth = apply(registry()['pmemo'][vibe]['expression'],y,['valence','arousal'])
            outer = np.zeros(len(y),dtype=int)
            pairs = holdout_pairs(truth,scores,groups,[('current','prior'),('current','shared')])
            tables[key] = {'rows':len(y),'components':len(set(groups)),
                'metrics':{m:protocol.metrics(truth,p,outer) for m,p in scores.items()},'paired':pairs}
            regression = any(c['ndcg']['difference']<-.01 or c['top_lift']['difference']<-.01 or c['mse']['difference']>.005 for c in pairs.values())
            gain = all(c['ndcg']['difference']>=.02 and c['top_lift']['difference']>=.01 and c['ndcg']['conditional_95_percent_interval'][0]>0 for c in pairs.values())
            conclusions[key] = 'inconclusive' if not supported else 'regression' if regression else 'confirmation' if gain else 'inconclusive'
    for key,value in prediction.items():
        label = key.split('__')[-1]
        if label not in ('valence','arousal'):continue
        j = ('valence','arousal').index(label)
        native[key] = {'rows':len(y),'mse':float(np.mean((value[keep]-y[:,j])**2)),
            'mae':float(np.mean(np.abs(value[keep]-y[:,j])))}
    report = {'created_at':stamp(),'conclusions':conclusions,'supported_sample_size':supported,
        'selected':128,'acquired':len(load(directory/'acquisition.json')['rows']),'aligned_predictions':len(rows),
        'eligible':int(keep.sum()),'components':len(set(groups)),
        'missing_trajectory_ids':[r['id'] for r,k in zip(rows,keep) if not k],
        'trajectory_audit':observed,'tables':tables,'native_errors':native,
        'outcome_order':{'predictions_sealed':load(directory/'predictions.receipt.json')['created_at'],'targets_opened':opened},
        'resources':{'acquisition':{k:v for k,v in load(directory/'acquisition.json').items() if k in ('seconds','audio_bytes','requested_bytes')},
            'extraction':load(directory/'features.json')['resources'],'prediction':load(directory/'prediction-trace.json'),
            'feature_bytes':sum(p.stat().st_size for p in (directory/'chunks').glob('*.json'))},
        'limitations':['Same-corpus recording confirmation; artist metadata absent, pretraining overlap unknown.',
            'VA mappings are fixed proxies, not native vibe or listening benefit labels. No Focus/Release reference.',
            'Native emotion heads and shared model have no observed native emotion targets here; native accuracy unsupported.',
            'Shared scores compete across six vibes; absolute-score mapped MSE and calibration do not establish ranking benefit.',
            'Conditional component bootstrap holds fixed models and observed corpus; no training uncertainty or independent domains.',
            'No fitting, tuning or winner selection; this population is now exposed for all later research.']}
    save(directory/'report.json',report)
    evidence.seal(directory,'evaluation',[directory/'report.json'])
    print(json.dumps({'conclusions':conclusions,'eligible':int(keep.sum()),'components':len(set(groups))}),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['freeze','acquire','extract','predict','evaluate','run','verify'])
    parser.add_argument('--directory',type=Path,default=DEFAULT)
    args = parser.parse_args(); directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action=='freeze':freeze(directory);return
        evidence.validate(directory,args.action!='verify')
        for action, function in [('acquire',acquire),('extract',extract),('predict',predict),('evaluate',evaluate)]:
            if args.action in (action,'run'):function(directory)
        if args.action=='verify':print(json.dumps(evidence.verify(directory)))


if __name__=='__main__':
    main()
