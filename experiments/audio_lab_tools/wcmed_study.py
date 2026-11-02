"""Recover exact WCMED source recordings and evaluate frozen transfer models."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
import urllib.request

import numpy as np
from scipy.stats import spearmanr, kendalltau
from threadpoolctl import threadpool_limits

from . import quality_round as evidence, individual_protocol as protocol
from .external_experiment import LAB, digest, probe, save
from .focus_experiment import arrays, check_receipt, inside, load
from .quality_reference_audio import Extractor, GRAPH, PACKAGE, provenance
from .quality_baselines import predict as adopted_predict
from .uplift_consensus import native_average
from .memo_confirmation import holdout_pairs

DEFAULT = LAB / 'local/wcmed-812-20261101/round-3'
DISCOVERY = LAB / 'local/datasets/discovery/research-20261101'
METADATA = LAB / 'local/datasets/ccmed-wcmed/source/CCMED_WCMED/WCMED'
DOCUMENT = LAB / 'WCMED_PLAN.md'
VIBES = ('cruise', 'relax', 'uplift', 'energize')


def read_csv(path):
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def components(rows):
    parents, known = list(range(len(rows))), {}
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for i, row in enumerate(rows):
        composer, work, performer, _ = row['songName'].split('_', 3)
        family = composer + '_' + re.sub(r'-\d+$', '', work)
        keys = [('recording', row['songName']), ('work', family), ('performer', performer)]
        if row.get('audio_sha256'):
            keys.append(('audio', row['audio_sha256']))
        for key in keys:
            if key in known:
                a, b = root(i), root(known[key])
                parents[max(a, b)] = min(a, b)
            known[key] = i
    return np.array([root(i) for i in range(len(rows))])


def freeze(directory, reuse=None):
    inputs = {'listing.html': DISCOVERY / 'smd-western.html',
              'intersection.json': DISCOVERY / 'wcmed-smd-intersection-audit.json',
              'metadata.csv': METADATA / 'WCMED-Metadata/WCMED_ Metadata.csv',
              'readme.txt': METADATA / 'Readme.txt',
              'prior-deam.json': LAB / 'local/memo-confirmation-710-20261101/round-1/inputs/prior-deam.json'}
    for axis in ('Valence', 'Arousal'):
        for kind in ('Ranking', 'Rating'):
            inputs[f'{axis.lower()}-{kind.lower()}.csv'] = METADATA / f'WCMED-{kind}s/WCMED_{axis}_{kind}.csv'
    for vibe in VIBES:
        inputs[f'adopted-{vibe}.json'] = LAB / f'local/encoder-comparison-708-20261101/round-3/inputs/adopted-{vibe}.json'
    rows = read_csv(inputs['metadata.csv'])
    references = [GRAPH, PACKAGE / 'METADATA', PACKAGE / 'RECORD']
    if reuse:
        evidence.verify(reuse, required_stages=['acquisition'], optional_stages=['features','predictions','evaluation'])
        inputs['reuse-acquisition.json'] = reuse / 'acquisition.json'
        references.extend([reuse / 'plan.json', reuse / 'acquisition.receipt.json',
            *[Path(r['audio']) for r in load(reuse / 'acquisition.json')['sources'].values()]])
        if (reuse / 'features.receipt.json').exists():
            inputs['reuse-features.json'] = reuse / 'features.json'
            inputs['reuse-features.npz'] = reuse / 'features.npz'
            references.extend([reuse / 'features.receipt.json',
                *[reuse / p for p in load(reuse / 'features.receipt.json')['outputs']]])
    p = evidence.freeze(directory, 812, {'document_sha256': digest(DOCUMENT),
        'metadata_rows': len(rows), 'metadata_components': len(set(components(rows))),
        'maximum_download_bytes': 1024**3, 'maximum_source_bytes': 64*1024**2,
        'acquisition_seconds': 1800, 'extraction_seconds': 7200,
        'bootstrap_draws': 1000, 'extraction': provenance(), 'target_fit': False},
        inputs, references=references,
        documents=[DOCUMENT, LAB.parent / 'investigations/wcmed_812.py'])
    print(json.dumps({'plan_sha256': digest(directory / 'plan.json'),
                      'metadata_components': p['protocol']['metadata_components']}), flush=True)


def acquire(directory):
    if (directory / 'acquisition.receipt.json').exists():
        raise ValueError('Preserve completed acquisition')
    started, received, outputs = time.monotonic(), 0, []
    candidates = load(directory / 'inputs/intersection.json')['recording_candidates']
    reuse = directory / 'inputs/reuse-acquisition.json'
    previous = load(reuse) if reuse.exists() else None
    report = {'sources': dict(previous['sources']) if previous else {}, 'attempts': [],
              'previous_attempts': previous['attempts'] if previous else [],
              'previous_acquisition_seconds': previous['seconds'] if previous else 0}
    received = previous['received_bytes'] if previous else 0
    (directory / 'audio').mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='downloads-', dir=directory) as temporary:
        for name, candidate in sorted(candidates.items()):
            if not candidate['urls']:
                continue
            if name in report['sources']:
                continue
            record = {'source': name, 'url': candidate['urls'][0]}
            try:
                if time.monotonic() - started > 1800:
                    raise ValueError('Acquisition time bound reached')
                target = directory / 'audio' / (name + '.mp3')
                temporary_file = Path(temporary) / 'download.mp3'
                request = urllib.request.Request(record['url'], headers={'User-Agent': 'TuneConsole-research/1'})
                size = 0
                with urllib.request.urlopen(request, timeout=20) as response, temporary_file.open('wb') as output:
                    record.update(status=response.status, final_url=response.url, headers=dict(response.headers))
                    while part := response.read(65536):
                        size += len(part)
                        received += len(part)
                        if size > 64*1024**2 or received > 1024**3 or time.monotonic()-started > 1800:
                            raise ValueError('Acquisition byte/time bound reached')
                        output.write(part)
                duration, channels = probe(temporary_file)
                temporary_file.replace(target)
                record.update(audio=str(target), audio_sha256=digest(target), bytes=size,
                              duration=duration, channels=channels)
                report['sources'][name] = record
                outputs.append(target)
            except Exception as error:
                record['error'] = type(error).__name__ + ': ' + str(error)
            report['attempts'].append(record)
            save(directory / 'acquisition-progress.json', report)
            print(json.dumps({'source': name, 'status': record.get('status'), 'bytes':record.get('bytes'),
                              'error':record.get('error')}), flush=True)
    rows, excluded = [], []
    metadata_rows = read_csv(directory / 'inputs/metadata.csv')
    frequencies = Counter(r['fileName'] for r in metadata_rows)
    for row in metadata_rows:
        start, end = float(row['start_point']), float(row['stop_point'])
        duration = float(row['selected_duration(s)'])
        if abs(end-start-duration) > .002:
            raise ValueError('Excerpt duration arithmetic mismatch')
        reason = ('ambiguous-metadata-id' if frequencies[row['fileName']] != 1 else
                  'missing-source' if row['songName'] not in report['sources'] else
                  'shorter-than-ten-seconds' if duration < 10 else None)
        if reason:
            excluded.append({'id': row['fileName'], 'reason': reason})
            continue
        source = report['sources'][row['songName']]
        if end > source['duration'] or start < 0:
            excluded.append({'id': row['fileName'], 'reason': 'source-interval-uncovered'})
            continue
        rows.append({**row, **{k:source[k] for k in ('audio','audio_sha256')},
            'id':row['fileName'], 'sample_start': start+(duration-10)/2,
            'edge_margin': (duration-10)/2})
    groups = components(rows) if rows else np.array([], dtype=int)
    for row, group in zip(rows, groups, strict=True):
        row['component'] = int(group)
    report.update(rows=rows, excluded=excluded, received_bytes=received,
        seconds=time.monotonic()-started, components=len(set(groups)), recordings=len({r['songName'] for r in rows}),
        original_fade_parameters='not supplied; reconstructed center intervals do not prove published excerpt parity')
    save(directory / 'acquisition.json', report)
    evidence.seal(directory, 'acquisition', [directory / 'acquisition.json', *outputs])
    print(json.dumps({k:report[k] for k in ('seconds','received_bytes','components','recordings')}),flush=True)


def extract(directory):
    check_receipt(directory, 'acquisition.receipt.json')
    rows = load(directory / 'acquisition.json')['rows']
    if len(rows) < 64:
        raise ValueError('Insufficient recovered cohort')
    if (directory / 'features.receipt.json').exists():
        raise ValueError('Preserve completed features')
    vectors, paths = [], []
    if (directory / 'inputs/reuse-features.json').exists():
        tick = time.monotonic()
        old = load(directory / 'inputs/reuse-features.json')
        old_x = arrays(directory / 'inputs/reuse-features.npz')['x']
        index = {(r['audio_sha256'],r['sample_start']):i for i,r in enumerate(old['rows'])}
        vectors = np.asarray([old_x[index[r['audio_sha256'],r['sample_start']]] for r in rows])
        np.savez_compressed(directory / 'features.npz',x=vectors)
        save(directory / 'features.json', {'rows':rows,'resources':{'new_chunks':0,'reused_chunks':len(rows),
            'elapsed_seconds':time.monotonic()-tick,'original_extraction':old['resources']}})
        evidence.seal(directory,'features',[directory / 'features.npz',directory / 'features.json'])
        return
    with Extractor(directory, seconds=7200) as encoder:
        for i, row in enumerate(rows):
            vector, path = encoder.chunk(row, row['sample_start'])
            vectors.append(vector)
            paths.append(path)
            if i % 20 == 0:
                print(json.dumps({'extracted':i+1,'total':len(rows)}),flush=True)
    np.savez_compressed(directory / 'features.npz', x=np.asarray(vectors))
    resources = encoder.resources
    assert resources['child_peak_rss_bytes'] <= 2*1024**3
    assert sum(p.stat().st_size for p in set(paths)) <= 64*1024**2
    save(directory / 'features.json', {'rows':rows, 'resources':resources})
    evidence.seal(directory, 'features', [directory / 'features.npz',directory / 'features.json', *set(paths)])


def va_scores(values):
    v, a = values[:, 0], values[:, 1]
    return {'cruise':(1-2*np.abs(a-.5)+v)/2, 'relax':(1-a+v)/2,'uplift':v,'energize':a}


def predict(directory):
    check_receipt(directory, 'features.receipt.json')
    if (directory / 'predictions.receipt.json').exists():
        raise ValueError('Preserve completed predictions')
    x = arrays(directory / 'features.npz')['x']
    chunks, counts = x[:,None,:], np.ones(len(x),dtype=int)
    prior = load(directory / 'inputs/prior-deam.json')
    va = native_average(prior['heads'], chunks, counts, independent=True)
    outputs = {'prior-va':va, **{'prior-'+v:s for v,s in va_scores(va).items()}}
    for vibe in VIBES:
        score, native = adopted_predict(load(directory / f'inputs/adopted-{vibe}.json'), chunks, counts)
        outputs['adopted-'+vibe] = score
        if vibe == 'cruise':
            for source, targets in native.items():
                if 'valence' in targets and 'arousal' in targets:
                    outputs['native-'+source] = np.column_stack([targets['valence'],targets['arousal']])
    assert all(np.isfinite(v).all() for v in outputs.values())
    np.savez_compressed(directory / 'predictions.npz', **outputs)
    evidence.seal(directory, 'predictions', [directory / 'predictions.npz'])


def correlation(y, score):
    return [float(spearmanr(y, score).statistic), float(kendalltau(y, score).statistic)]


def evaluate(directory):
    check_receipt(directory, 'predictions.receipt.json')
    started = time.monotonic()
    rows = load(directory / 'features.json')['rows']
    pred = arrays(directory / 'predictions.npz')
    y = []
    conversions = {}
    for axis in ('valence','arousal'):
        ranking = {r['name']:int(r['ranking']) for r in read_csv(directory / f'inputs/{axis}-ranking.csv')}
        rating = {r['name']:float(r['rating']) for r in read_csv(directory / f'inputs/{axis}-rating.csv')}
        assert sorted(ranking.values()) == list(range(1,401))
        conversion_error = max(abs(rating[k]-(1-2*(rank-1)/400)) for k,rank in ranking.items())
        assert conversion_error < .00001
        conversions[axis] = {'maximum_supplied_rating_conversion_error': conversion_error,
                             'highest_rank':1,'rank_space_scale':[0,1]}
        y.append([(400-ranking[r['id']])/399 for r in rows])
    y = np.asarray(y).T
    groups = np.asarray([r['component'] for r in rows])
    recordings = np.asarray([r['songName'] for r in rows])
    blocks = [np.flatnonzero(groups==g) for g in sorted(set(groups))]
    report = {'rank_conversion':conversions,'native':{},'vibes':{},'rows':len(rows),
              'components':len(blocks),'recordings':len(set(recordings))}
    rng = np.random.default_rng(8121101)
    samples = [np.concatenate([blocks[i] for i in rng.integers(len(blocks),size=len(blocks))]) for _ in range(1000)]
    for name, values in pred.items():
        if values.ndim != 2:
            continue
        report['native'][name] = {}
        for column, axis in enumerate(('valence','arousal')):
            point = correlation(y[:,column], values[:,column])
            draws = np.asarray([correlation(y[ix,column], values[ix,column]) for ix in samples])
            report['native'][name][axis] = {'spearman':point[0],'kendall':point[1],
                'conditional_component_intervals':np.quantile(draws,[.025,.975],axis=0).T.tolist()}
    for vibe, truth in va_scores(y).items():
        scores = {k:pred[k+'-'+vibe] for k in ('adopted','prior')}
        report['vibes'][vibe] = {'metrics':{k:protocol.metrics(truth,p,np.zeros(len(y),dtype=int)) for k,p in scores.items()},
            'paired_components':holdout_pairs(truth,scores,groups,[('adopted','prior')]),
            'paired_recordings':holdout_pairs(truth,scores,recordings,[('adopted','prior')])}
        keep = np.array([r['edge_margin'] >= 1 for r in rows])
        report['vibes'][vibe]['margin_one_second'] = {'rows':int(keep.sum()),
            'metrics':{k:protocol.metrics(truth[keep],p[keep],np.zeros(keep.sum(),dtype=int)) for k,p in scores.items()}}
    report.update(seconds=time.monotonic()-started, decision='retain-frozen-models; benchmark coverage only',
        limitations=['Reconstructed interval, unknown original attack/decay; no published processed-audio parity.',
            'Global ordinal ranks, not absolute VA calibration or raw comparison votes.',
            'Cruise/Relax rank-space composites are exploratory; no Focus/Release truth.',
            'Transitive performer/work clusters; unknown corpus composition and encoder-pretraining overlap.',
            'No WCMED fitting, calibration, target-driven model selection or production candidate.'])
    save(directory / 'report.json',report)
    evidence.seal(directory, 'evaluation', [directory / 'report.json'])
    print(json.dumps({'rows':len(rows),'components':len(blocks),'decision':report['decision']}),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['freeze','acquire','extract','predict','evaluate','verify'])
    parser.add_argument('--directory',type=Path,default=DEFAULT)
    parser.add_argument('--reuse-acquisition',type=Path)
    args=parser.parse_args()
    directory=inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze': freeze(directory, inside(args.reuse_acquisition) if args.reuse_acquisition else None)
        elif args.action == 'verify':
            print(json.dumps(evidence.verify(directory,required_stages=['acquisition','features','predictions','evaluation'])))
        else:
            evidence.validate(directory,execution=True)
            {'acquire':acquire,'extract':extract,'predict':predict,'evaluate':evaluate}[args.action](directory)


if __name__ == '__main__': main()
