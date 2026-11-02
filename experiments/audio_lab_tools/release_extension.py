"""Bounded Jamendo acquisition and artist-sequestered aggression experiment #709."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import resource
import tarfile
from tempfile import TemporaryDirectory
import time
import urllib.request

import numpy as np
from threadpoolctl import threadpool_limits

from . import individual_protocol as protocol, quality_round as evidence
from .external_experiment import LAB, digest, probe, save, stamp
from .external_sources import JAMENDO_SOURCE, JAMENDO_TASKS, checksums
from .focus_experiment import arrays, check_receipt, head_predictions, inside, load
from .individual_experiment import compact
from .mapped_vibe_inference import replay
from .quality_reference_audio import Extractor, provenance, GRAPH, PACKAGE
from .relax_experiment import global_groups

DEFAULT = LAB / 'local/release-extension-709-20261101/round-3'
PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
DOCUMENT = LAB / 'RELEASE_EXTENSION_PLAN.md'
JAM, SOUND = 'jamendo-human-4shards', 'soundtracks-all'
RAW = JAMENDO_SOURCE / 'derived/music-classification-annotations/music-classification-annotations-raw.tsv'
DOWNLOAD = JAMENDO_SOURCE / 'data/download'
LIMIT = 5 * 1024 ** 3 // 4
SEED = 'release-extension-709-v1/'


def identities(raw, track_hashes, old_artists):
    """Read identity/completeness only; never compute a held-out vote fraction."""
    result = []
    with raw.open() as stream:
        next(stream)
        for line in stream:
            fields = line.rstrip('\n').split('\t')
            path = fields[3].replace('.mp3', '.low.mp3')
            if path.split('/')[0] not in ('70', '63') or fields[1] in old_artists or path not in track_hashes:
                continue
            tasks = dict(value.split('---', 1) for value in fields[5:])
            if any(not tasks.get('mood_' + name) or any(v not in (name, 'not_' + name)
                for v in tasks['mood_' + name].split(',')) for name in JAMENDO_TASKS):
                continue
            result.append({'id': fields[0], 'source_artist_id': fields[1], 'source_album_id': fields[2],
                'source_path': fields[3], 'path': path, 'group': 'jamendo:' + fields[1],
                'artist': '', 'title': '', 'annotation_duration': float(fields[4]),
                'audio_sha256': track_hashes[path], 'annotation_unit': 'whole_song_explicit_human_binary_vote_fraction'})
    if len({r['id'] for r in result}) != len(result):
        raise ValueError('Duplicate annotation recording ID')
    return result


def artist_partition(rows):
    units = defaultdict(list)
    for row in rows:
        units[row['component']].append(row)
    ordered = sorted(units, key=lambda g: hashlib.sha256((SEED + '/'.join(sorted(
        {r['source_artist_id'] for r in units[g]}))).encode()).hexdigest())
    training = set(ordered[:len(ordered) // 2])
    return [{**r, 'partition': 'train' if r['component'] in training else 'evaluation'} for r in rows]


def read_targets(raw, ids):
    ids = set(ids)
    result = {}
    with raw.open() as stream:
        next(stream)
        for line in stream:
            fields = line.rstrip('\n').split('\t')
            if fields[0] not in ids:
                continue
            values = dict(value.split('---', 1) for value in fields[5:])['mood_aggressive'].split(',')
            if not values or any(v not in ('aggressive', 'not_aggressive') for v in values):
                raise ValueError('Missing/invalid native aggression response')
            result[fields[0]] = {'target': values.count('aggressive') / len(values), 'responses': len(values)}
    if set(result) != ids:
        raise ValueError('Missing requested annotation rows')
    return result


def freeze(directory):
    inputs = {'annotations.tsv': RAW, 'tar-checksums.txt': DOWNLOAD / 'autotagging_moodtheme_audio-low_sha256_tars.txt',
        'track-checksums.txt': DOWNLOAD / 'autotagging_moodtheme_audio-low_sha256_tracks.txt',
        'adopted.json': LAB.parent.parent / 'src/yt_playlist/rec/data/release-vibes.json'}
    for source in (JAM, SOUND):
        for ext in ('npz', 'json'):
            inputs[source + '.' + ext] = PREVIOUS / f'inputs/{source}.{ext}'
    old = load(inputs[JAM + '.json'])['rows']
    if load(inputs['adopted.json'])['model'].get('alpha') != 1000.:
        raise ValueError('Exact adopted aggression alpha changed')
    old_artists = {r['group'].removeprefix('jamendo:') for r in old}
    selected = identities(RAW, checksums(inputs['track-checksums.txt']), old_artists)
    rows = list(selected)
    boundaries = {}
    for source in (JAM, SOUND):
        source_rows = load(inputs[source + '.json'])['rows']
        boundaries[source] = slice(len(rows), len(rows) + len(source_rows))
        rows.extend(source_rows)
    known_start = len(rows)
    for path in sorted((LAB / 'local/per-vibe-20261029/all-sources-1/inputs').glob('*.json')):
        inputs['identities/' + path.name] = path
        rows.extend(load(path).get('rows', []))
    local = LAB / 'local/individual-20261030/local-1/labels.json'
    inputs['local-identities.json'] = local
    local_start = len(rows)
    rows.extend(load(local)['rows'])
    groups = global_groups(rows)
    known = set(groups[known_start:])
    selected = [{**r, 'component': int(groups[i]), 'eligible': groups[i] not in known}
                for i, r in enumerate(selected)]
    assigned = artist_partition([r for r in selected if r['eligible']])
    by_id = {r['id']: r for r in assigned}
    selected = [by_id.get(r['id'], {**r, 'partition': 'excluded_known_component'}) for r in selected]
    specification = {'document_sha256': digest(DOCUMENT), 'shards': ['70', '63'],
        'transfer_byte_limit': LIMIT, 'selection': SEED, 'alpha': 1000., 'bootstrap_draws': 1000,
        'extraction': provenance(), 'minimum_test_recordings': 20, 'minimum_test_components': 15,
        'acceptance': {'ndcg_gain': .02, 'lift_gain': .01, 'maximum_mse_increase': .005, 'transfer_ranking_loss': .01},
        'budgets': {'acquisition_seconds': 1800, 'extraction_fit_seconds': 3600, 'rss_bytes': 1024 ** 3},
        'outcome_order': 'Metadata/completeness only at freeze. Training targets after acquisition. Evaluation vote fractions only after predictions.receipt.json.'}
    plan = evidence.freeze(directory, 709, specification, inputs,
        references=[GRAPH, PACKAGE / 'METADATA', PACKAGE / 'RECORD', LAB / 'RELEASE_LABEL_AUDIT.md',
            LAB / 'local/release-extension-709-20261101/round-1/plan.json',
            LAB / 'local/release-extension-709-20261101/round-1/acquisition.json'],
        documents=[DOCUMENT, Path(__file__).with_name('test_release_extension.py')])
    selection = {'rows': selected, 'components': {s: groups[b].tolist() for s, b in boundaries.items()},
        'local_components': sorted(set(map(int, groups[local_start:]))),
        'known_component_exclusions': [r['id'] for r in selected if not r['eligible']],
        'exposure': 'Complete raw annotation table was available and parsed in earlier audits. New audio/model holdout, not a fresh annotation population. Unknown artist aliases and backbone overlap unresolved.'}
    save(directory / 'inputs/selection.json', selection)
    plan['inputs']['inputs/selection.json'] = digest(directory / 'inputs/selection.json')
    save(directory / 'plan.json', plan)
    evidence.validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
        'tracks': len(selected), 'artists': len({r['source_artist_id'] for r in selected}),
        'partition': {k: sum(r['partition'] == k for r in selected) for k in ('train', 'evaluation', 'excluded_known_component')}}), flush=True)


def acquire(directory):
    if (directory / 'acquisition.receipt.json').exists():
        check_receipt(directory, 'acquisition.receipt.json')
        return
    selected = load(directory / 'inputs/selection.json')['rows']
    wanted = {r['path']: r for r in selected if r['eligible']}
    hashes = checksums(directory / 'inputs/tar-checksums.txt')
    total, records, archives, failures = 0, [], [], []
    started = time.monotonic()
    with TemporaryDirectory(prefix='archives-', dir=directory) as temporary:
        for shard in ('70', '63'):
            name = f'autotagging_moodtheme_audio-low-{shard}.tar'
            url = 'https://essentia.upf.edu/documentation/datasets/mtg-jamendo/autotagging_moodtheme/audio-low/' + name
            path = Path(temporary) / name
            try:
                request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity', 'User-Agent': 'TuneConsole-research/1'})
                with urllib.request.urlopen(request, timeout=45) as response:
                    declared = int(response.headers['Content-Length'])
                    if response.status != 200 or declared > LIMIT - total:
                        raise ValueError('Archive exceeds remaining combined transfer bound')
                    received, announced = 0, 0
                    with path.open('wb') as outgoing:
                        while received < declared:
                            if time.monotonic() - started > 1800:
                                raise TimeoutError('Combined acquisition time bound')
                            block = response.read(min(1024 ** 2, declared - received, LIMIT - total))
                            if not block:
                                break
                            total += len(block)
                            received += len(block)
                            if total > LIMIT:
                                raise ValueError('Combined transfer byte bound exceeded')
                            outgoing.write(block)
                            if received - announced >= 64 * 1024 ** 2:
                                print(json.dumps({'download': shard, 'bytes': received, 'total_bytes': total}), flush=True)
                                announced = received
                if received != declared or digest(path) != hashes[name]:
                    raise ValueError('Whole archive size/SHA256 verification failed')
                archives.append({'name': name, 'url': url, 'bytes': received, 'sha256': digest(path)})
                with tarfile.open(path) as archive:
                    for member in archive:
                        if member.name not in wanted:
                            continue
                        if not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts:
                            raise ValueError('Unsafe selected tar member')
                        destination = directory / 'audio' / member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as incoming, destination.open('wb') as outgoing:
                            while block := incoming.read(1024 ** 2):
                                outgoing.write(block)
                        row = wanted[member.name]
                        if digest(destination) != row['audio_sha256']:
                            destination.unlink()
                            raise ValueError('Selected track official SHA256 mismatch')
                        records.append({**row, 'audio': str(destination), 'bytes': destination.stat().st_size})
                path.unlink()
            except (OSError, ValueError, tarfile.TarError) as error:
                failures.append({'shard': shard, 'error': str(error)})
                print(json.dumps({'acquisition_failure': shard, 'error': str(error)}), flush=True)
    obtained = {r['id'] for r in records}
    report = {'created_at': stamp(), 'rows': records, 'archives': archives, 'failures': failures,
        'missing_ids': [r['id'] for r in selected if r['eligible'] and r['id'] not in obtained],
        'network_bytes': total, 'seconds': time.monotonic() - started, 'retries': 0}
    save(directory / 'acquisition.json', report)
    evidence.seal(directory, 'acquisition', [directory / 'acquisition.json', *[Path(r['audio']) for r in records]])
    print(json.dumps({'acquired': len(records), 'network_bytes': total, 'failures': failures}), flush=True)


def extract(directory):
    check_receipt(directory, 'acquisition.receipt.json')
    if (directory / 'extraction.receipt.json').exists():
        check_receipt(directory, 'extraction.receipt.json')
        return
    rows, rejected, values, caches = [], [], [], []
    with Extractor(directory, seconds=3600) as worker:
        for row in load(directory / 'acquisition.json')['rows']:
            try:
                duration, _ = probe(row['audio'])
                if duration < 10 or abs(duration - row['annotation_duration']) > max(3, .02 * row['annotation_duration']):
                    raise ValueError('Unusable duration or annotation/audio version discrepancy')
                start = (duration - 10) / 2
                vector, cache = worker.chunk(row, start)
                rows.append({**row, 'sample_start': start, 'sample_seconds': 10, 'recording_duration': duration,
                             'coverage': 'Center 10 seconds approximate whole-song annotations; no excerpt-specific judgments.'})
                values.append(vector)
                caches.append(cache)
            except (ValueError, OSError) as error:
                rejected.append({'id': row['id'], 'error': str(error)})
    np.savez_compressed(directory / 'features.npz', x=np.asarray(values).reshape(-1, 200))
    save(directory / 'extraction.json', {'rows': rows, 'rejected': rejected, 'resources': worker.resources})
    evidence.seal(directory, 'extraction', [directory / 'features.npz', directory / 'extraction.json', *caches])
    print(json.dumps({'usable': len(rows), 'rejected': rejected, 'resources': worker.resources}), flush=True)


def fit_head(x, y):
    return protocol.learner.serial(protocol.learner.fit_models(x, y[:, None], [{'family': 'ridge', 'alpha': 1000.}])[0])


def predict(directory):
    check_receipt(directory, 'extraction.receipt.json')
    if (directory / 'predictions.receipt.json').exists():
        check_receipt(directory, 'predictions.receipt.json')
        return
    started = time.monotonic()
    selection = load(directory / 'inputs/selection.json')
    rows = load(directory / 'extraction.json')['rows']
    x = arrays(directory / 'features.npz')['x']
    train = np.array([r['partition'] == 'train' for r in rows], dtype=bool)
    target = read_targets(directory / 'inputs/annotations.tsv', [r['id'] for r in rows if r['partition'] == 'train'])
    new_y = np.array([target[r['id']]['target'] for r in rows if r['partition'] == 'train'])
    old = arrays(directory / f'inputs/{JAM}.npz')
    meta = load(directory / f'inputs/{JAM}.json')
    old_y = old['y'][:, meta['target_names'].index('aggressive')]
    old_groups = np.array(selection['components'][JAM])
    sound = arrays(directory / f'inputs/{SOUND}.npz')
    sound_groups = np.array(selection['components'][SOUND])
    held = {r['component'] for r in rows if r['partition'] == 'evaluation'}
    protected = held | set(selection['local_components'])
    adopted = load(directory / 'inputs/adopted.json')['model']
    result, traces, outputs = {}, {}, []
    for domain, exclusions in (('reserved', protected), ('soundtracks', protected | set(sound_groups))):
        keep = np.array([g not in exclusions for g in old_groups])
        extension = train & np.array([r['component'] not in exclusions for r in rows], dtype=bool)
        selected_y = np.array([target[r['id']]['target'] for r, yes in zip(rows, extension, strict=True) if yes])
        original = fit_head(old['embedding'][keep], old_y[keep])
        extended = fit_head(np.concatenate([old['embedding'][keep], x[extension]]), np.concatenate([old_y[keep], selected_y]))
        for name, model in (('original', original), ('extended', extended)):
            path = directory / f'models/{domain}-{name}.json'
            compact(path, model)
            outputs.append(path)
        traces[domain] = {'original_ids': np.array([r['id'] for r in meta['rows']])[keep].tolist(),
            'original_groups': old_groups[keep].tolist(), 'extension_ids': [r['id'] for r, yes in zip(rows, extension, strict=True) if yes],
            'extension_groups': [r['component'] for r, yes in zip(rows, extension, strict=True) if yes],
            'excluded_components': [int(g) for g in sorted(exclusions)], 'alpha': 1000.}
        if domain == 'reserved':
            inputs = [('historical', x)]
        else:
            inputs = [('historical', sound['embedding']), ('browser', sound['browser'])]
        for prep, xx in inputs:
            for name, model in (('adopted', adopted), ('original', original), ('extended', extended)):
                result[f'{domain}__{prep}__{name}'] = replay(model, xx)[:, 0]
    result['new_ids'] = np.array([r['id'] for r in rows])
    np.savez_compressed(directory / 'predictions.npz', **result)
    save(directory / 'prediction-trace.json', {'created_at': stamp(), 'training': traces,
        'training_target_ids_opened': sorted(target), 'evaluation_targets_opened': False,
        'seconds': time.monotonic() - started, 'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024})
    evidence.seal(directory, 'predictions', [directory / 'predictions.npz', directory / 'prediction-trace.json', *outputs])
    print(json.dumps({'sealed_predictions': len(rows), 'training_rows': int(train.sum()), 'held_out_targets_opened': False}), flush=True)


def evaluate(directory):
    check_receipt(directory, 'predictions.receipt.json')
    started = time.monotonic()
    rows = load(directory / 'extraction.json')['rows']
    prediction = arrays(directory / 'predictions.npz')
    held = np.array([r['partition'] == 'evaluation' for r in rows], dtype=bool)
    opened_at = stamp()
    targets = read_targets(directory / 'inputs/annotations.tsv', [r['id'] for r in rows if r['partition'] == 'evaluation'])
    y = np.array([targets[r['id']]['target'] for r in rows if r['partition'] == 'evaluation'])
    groups = np.array([r['component'] for r in rows])[held]
    scores = {m: prediction['reserved__historical__' + m][held] for m in ('adopted', 'original', 'extended')}
    comparisons = [('extended', c) for c in ('adopted', 'original')]
    tables = {}
    if len(y) and len(set(groups)):
        tables['reserved'] = {'historical': {'rows': len(y), 'groups': len(set(groups)),
            'metrics': {m: protocol.metrics(y, v, np.zeros(len(y), dtype=int)) for m, v in scores.items()},
            'paired': protocol.paired(y, scores, np.zeros(len(y), dtype=int), groups, comparisons, 1000)}}
    meta = load(directory / f'inputs/{SOUND}.json')
    data = arrays(directory / f'inputs/{SOUND}.npz')
    y_sound = data['y'][:, meta['target_names'].index('anger')]
    from .external_training import folds
    sound_groups = np.array(load(directory / 'inputs/selection.json')['components'][SOUND])
    outer = folds(sound_groups, count=5, seed=protocol.SEED)
    tables['soundtracks'] = {}
    for prep in ('historical', 'browser'):
        s = {m: prediction[f'soundtracks__{prep}__{m}'] for m in ('adopted', 'original', 'extended')}
        tables['soundtracks'][prep] = {'rows': len(y_sound), 'groups': len(set(sound_groups)),
            'metrics': {m: protocol.metrics(y_sound, v, outer) for m, v in s.items()},
            'paired': protocol.paired(y_sound, s, outer, sound_groups, comparisons, 1000)}
    reasons = []
    supported = len(y) >= 20 and len(set(groups)) >= 15 and np.ptp(y) > 0 if len(y) else False
    if not supported:
        reasons.append('Reserved artist support below 20 recordings / 15 components or no native variation.')
    if supported:
        for control, cell in tables['reserved']['historical']['paired'].items():
            if (cell['ndcg']['difference'] < .02 or cell['top_lift']['difference'] < .01
                or cell['ndcg']['conditional_95_percent_interval'][0] <= 0 or cell['mse']['difference'] > .005):
                reasons.append('Reserved native gain/uncertainty/error rule failed: ' + control)
    for prep, table in tables['soundtracks'].items():
        for control, cell in table['paired'].items():
            if cell['ndcg']['difference'] < -.01 or cell['top_lift']['difference'] < -.01 or cell['mse']['difference'] > .005:
                reasons.append('Soundtracks regression ceiling: ' + prep + '/' + control)
    trace = load(directory / 'prediction-trace.json')
    acquisition = load(directory / 'acquisition.json')
    extraction = load(directory / 'extraction.json')
    resources = {'acquisition_seconds': acquisition['seconds'], 'acquisition_bytes': acquisition['network_bytes'],
        'extraction': extraction['resources'], 'fit_seconds': trace['seconds'], 'evaluation_seconds': time.monotonic() - started,
        'python_peak_rss_bytes': max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024, trace['peak_rss_bytes']),
        'largest_artifact_bytes': max(p.stat().st_size for p in (directory / 'models').glob('*.json'))}
    if max(resources['python_peak_rss_bytes'], extraction['resources']['child_peak_rss_bytes']) > 1024 ** 3 or acquisition['network_bytes'] > LIMIT or (
        extraction['resources']['elapsed_seconds'] + trace['seconds'] + resources['evaluation_seconds'] > 3600):
        reasons.append('Resource bound failed.')
    report = {'created_at': stamp(), 'decision': {'recommendation': 'retain' if reasons else 'candidate_for_review',
        'support': 'supported' if supported else 'inconclusive', 'reasons': reasons}, 'tables': tables, 'resources': resources,
        'usable_records': len(rows), 'usable_artists': len({r['source_artist_id'] for r in rows}),
        'training_records': int((~held).sum()), 'evaluation_records': int(held.sum()),
        'evaluation_artists': len({r['source_artist_id'] for r, yes in zip(rows, held, strict=True) if yes}),
        'evaluation_ids': [r['id'] for r, yes in zip(rows, held, strict=True) if yes],
        'evaluation_response_counts': {k: v['responses'] for k, v in targets.items()},
        'outcome_order': {'predictions_sealed_at': load(directory / 'predictions.receipt.json')['created_at'],
                          'heldout_outcomes_opened_at': opened_at},
        'limitations': ['Whole-song votes approximated by center ten-second excerpts.',
            'Raw annotation rows were available in prior audits; new audio/model holdout, not new annotation population.',
            'Missing artist names/unknown aliases and encoder pretraining overlap unresolved.',
            'Soundtracks anger is previously inspected transfer evidence; aggression/anger do not establish cathartic benefit.']}
    save(directory / 'report.json', report)
    evidence.seal(directory, 'evaluation', [directory / 'report.json'])
    print(json.dumps({'decision': report['decision'], 'usable': len(rows), 'evaluation_records': int(held.sum()), 'resources': resources}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'acquire', 'extract', 'predict', 'evaluate', 'run', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
            return
        evidence.validate(directory, args.action != 'verify')
        for action, function in [('acquire', acquire), ('extract', extract), ('predict', predict), ('evaluate', evaluate)]:
            if args.action in (action, 'run'):
                function(directory)
        if args.action == 'verify':
            print(json.dumps(evidence.verify(directory)))


if __name__ == '__main__':
    main()
