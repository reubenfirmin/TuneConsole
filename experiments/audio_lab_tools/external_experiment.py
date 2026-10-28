"""Offline external-audio experiments. Never opens an application database.

Run with .venv/bin/python -m experiments.audio_lab_tools.external_experiment.
All generated data, PCM scratch files, and candidate models stay under local/.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile
import tarfile
import csv
import fcntl
import select

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / 'experiments/audio_lab'
DATA = LAB / 'local/datasets'
OUT = LAB / 'local/external-20261028'
POLICY = 'external-center-10s-ffmpeg-1'
SOUND_TARGETS = ('valence', 'energy', 'tension', 'anger', 'fear', 'happy', 'sad', 'tender')
EMOTIFY_TARGETS = ('amazement', 'solemnity', 'tenderness', 'nostalgia', 'calmness',
                   'power', 'joyful_activation', 'tension', 'sadness')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.writing')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def normalize(text):
    return re.sub(r'[^a-z0-9]+', '', text.lower())


def table(path, encoding='utf-8-sig'):
    with Path(path).open(encoding=encoding, newline='') as stream:
        return [{key.strip().lower(): value.strip() for key, value in row.items()
                 if key is not None} for row in csv.DictReader(stream)]


def unpack(archive, target, *, selected=None):
    """Extract ordinary audio and annotation files, never archive links."""
    if not archive.exists():
        raise FileNotFoundError(archive)
    target.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                name = Path(member.filename)
                if member.is_dir() or '__MACOSX' in name.parts or name.name.startswith('.'):
                    continue
                if name.is_absolute() or '..' in name.parts:
                    raise ValueError('Unsafe archive path')
                if selected and not selected(member.filename):
                    continue
                destination = target / name
                if destination.exists() and destination.stat().st_size == member.file_size:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, destination.open('wb') as outgoing:
                    import shutil
                    shutil.copyfileobj(incoming, outgoing)
    else:
        with tarfile.open(archive) as source:
            for member in source:
                name = Path(member.name)
                if not member.isfile() or (selected and not selected(member.name)):
                    continue
                if name.is_absolute() or '..' in name.parts:
                    raise ValueError('Unsafe archive path')
                destination = target / name
                if destination.exists() and destination.stat().st_size == member.size:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, destination.open('wb') as outgoing:
                    import shutil
                    shutil.copyfileobj(incoming, outgoing)


def audio_files(directory):
    return sorted(p for p in directory.rglob('*') if p.suffix.lower() in ('.mp3', '.wav', '.flac')
                  and '__MACOSX' not in p.parts and not p.name.startswith('.'))


def soundtrack_rows(all_sets=False):
    base = DATA / 'soundtracks'
    if all_sets:
        unpack(base / 'Set1.zip', base / 'source', selected=lambda s: s.lower().endswith('.mp3'))
    meta = {int(row['nro']): row for row in table(base / 'set2_tracklist.csv')}
    set2 = table(base / 'mean_ratings_set2.csv')
    # Conservatively merge all linked excerpts and films, including duplicated
    # stimuli assigned to different intended-emotion categories.
    parents = {}

    def find(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]

    def union(a, b):
        parents[find(a)] = find(b)

    for row in set2:
        number = int(row['number'])
        group = 'film:' + normalize(meta[number]['album name'])
        union('set2:' + str(number), group)
        union('set1:' + str(int(row['link'])), group)
        union(group, 'film:' + normalize(row['soundtrack']))
    # Source documentation identifies these as duplicate/related recordings.
    for left, right in [(10, 95), (84, 88), (9, 17)]:
        union('set2:' + str(left), 'set2:' + str(right))
    result = []
    for label_set, rows in ([('set1', table(base / 'mean_ratings_set1.csv'))] if all_sets else []) + [('set2', set2)]:
        folder = base / 'source' / ('Set1' if label_set == 'set1' else 'set2')
        for row in rows:
            number = int(row['number'])
            path = folder / f'{number:03d}.mp3'
            if not path.exists():
                raise FileNotFoundError(path)
            result.append({'id': f'soundtracks:{label_set}:{number:03d}', 'audio': str(path),
                           'group': 'soundtracks:' + find(label_set + ':' + str(number)),
                           'target_names': list(SOUND_TARGETS),
                           'targets': [(float(row[key]) - 1) / 8 for key in SOUND_TARGETS],
                           'native_scale': [1, 9], 'annotation_unit': 'excerpt',
                           'title': meta[number]['album name'] if label_set == 'set2' else '',
                           'artist': '', 'source_set': label_set})
    return result


def cal500_rows():
    base = DATA / 'cal500'
    unpack(base / 'CAL500_32kps.tar', base / 'audio', selected=lambda s: s.lower().endswith('.mp3'))
    files = {p.stem.lower(): p for p in audio_files(base / 'audio')}
    annotations = base / 'annotations-source'
    names = (annotations / 'songNames.txt').read_text().splitlines()
    targets = (annotations / 'vocab.txt').read_text().splitlines()
    values = np.loadtxt(annotations / 'softAnnotations.txt', delimiter=',')
    rows, missing = [], []
    for index, name in enumerate(names):
        path = files.get(name.lower())
        if path is None:
            missing.append(name)
            continue
        artist, _, title = name.partition('-')
        rows.append({'id': 'cal500:' + name, 'audio': str(path), 'group': 'cal500:' + normalize(artist),
                     'target_names': targets, 'targets': values[index].tolist(),
                     'native_scale': [0, 1], 'annotation_unit': 'whole_song_weak_label',
                     'artist': artist.replace('_', ' '), 'title': title.replace('_', ' ')})
    save(OUT / 'cal500.join-audit.json', {'annotation_rows': len(names), 'audio_files': len(files),
         'matched_rows': len(rows), 'unavailable_audio': missing,
         'policy': 'exact published filename join; unavailable recordings excluded, no inferred matches'})
    return rows


def emotify_rows():
    base = DATA / 'emotify'
    unpack(base / 'emotifymusic.zip', base / 'audio', selected=lambda s: s.lower().endswith('.mp3'))
    genre_offset = {'classical': 0, 'rock': 100, 'electronic': 200, 'pop': 300}
    files = {genre_offset[p.parent.name] + int(p.stem): p
             for p in audio_files(base / 'audio') if p.stem.isdigit()}
    annotations = defaultdict(list)
    for row in table(base / 'data.csv'):
        annotations[int(row['track id'])].append(row)
    result = []
    for number, ratings in sorted(annotations.items()):
        if number not in files:
            raise ValueError('Emotify audio identity missing: ' + str(number))
        result.append({'id': f'emotify:{number}', 'audio': str(files[number]),
                       'group': f'emotify:{number}', 'artist': '', 'title': '',
                       'target_names': list(EMOTIFY_TARGETS),
                       'targets': [sum(float(row[key]) for row in ratings) / len(ratings) for key in EMOTIFY_TARGETS],
                       'rater_count': len(ratings), 'native_scale': [0, 1],
                       'annotation_unit': 'excerpt_induced_emotion'})
    return result


def pmemo_rows():
    base = DATA / 'pmemo'
    def selected(name):
        lower = name.lower()
        return lower.endswith('.mp3') or Path(lower).name in (
            'a_static.csv', 'v_static.csv', 'metadata.csv', 'chorus.csv')
    unpack(base / 'PMEmo.zip', base / 'source', selected=selected)
    files = audio_files(base / 'source')
    paths = {int(p.stem): p for p in files if p.stem.isdigit()}
    arousal = list((base / 'source').rglob('A_static.csv'))
    valence = list((base / 'source').rglob('V_static.csv'))
    if len(arousal) != 1 or len(valence) != 1:
        raise ValueError('Locate PMEmo native static annotations')
    a_labels = {int(row['musicid']): row for row in table(arousal[0])}
    v_labels = {int(row['musicid']): row for row in table(valence[0])}
    if a_labels.keys() != v_labels.keys():
        raise ValueError('PMEmo arousal/valence identities differ')
    metadata = list((base / 'source').rglob('metadata.csv'))
    identities = {}
    if metadata:
        identities = {row['song_id']: row for row in table(metadata[0])}
    rows = []
    for number in sorted(a_labels):
        if number not in paths:
            raise ValueError('PMEmo audio identity missing: ' + str(number))
        identity = identities.get(str(number), {})
        artist = identity.get('artist', '')
        rows.append({'id': f'pmemo:{number}', 'audio': str(paths[number]),
                     'group': 'pmemo:' + (normalize(artist.split('+')[0]) if artist else str(number)),
                     'artist': artist, 'title': identity.get('song_title', ''),
                     'target_names': ['arousal', 'valence'],
                     'targets': [float(a_labels[number]['mean_a']), float(v_labels[number]['mean_v'])],
                     'native_scale': [0, 1], 'annotation_unit': 'chorus_excerpt'})
    return rows


def prepare(dataset):
    from .external_emma import prepare_rows as emma_rows
    from .external_merge import prepare_rows as merge_rows
    from .external_vgmidi import prepare_rows as vgmidi_rows
    from .external_sources import coimbra_rows, deam_rows, jamendo_rows, quadrant_rows
    builders = {'soundtracks-set2': lambda: soundtrack_rows(False),
                'soundtracks-all': lambda: soundtrack_rows(True),
                'cal500': cal500_rows, 'emotify': emotify_rows, 'pmemo': pmemo_rows,
                'vgmidi-original': vgmidi_rows, 'coimbra-mirex-like': coimbra_rows,
                'deam': deam_rows, 'jamendo-human-00': jamendo_rows,
                '4q-emotion': quadrant_rows, 'emma-preview-matched': emma_rows,
                'merge-quadrants': lambda: merge_rows('quadrants'),
                'merge-tag-va': lambda: merge_rows('tag-va'),
                'jamendo-human-4shards': lambda: jamendo_rows(('00', '07', '23', '53'))}
    rows = builders[dataset]()
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate example identifiers')
    for row in rows:
        if not row['targets'] or not all(np.isfinite(y) and 0 <= y <= 1 for y in row['targets']):
            raise ValueError('Invalid native targets: ' + row['id'])
        row['audio_sha256'] = digest(row['audio'])
    # Identical encoded audio must share a fold even if source identities differ.
    groups, parents = {}, {}
    def find(group):
        parents.setdefault(group, group)
        if parents[group] != group:
            parents[group] = find(parents[group])
        return parents[group]
    for row in rows:
        groups.setdefault(row['audio_sha256'], row['group'])
        parents[find(row['group'])] = find(groups[row['audio_sha256']])
    for row in rows:
        row['group'] = find(row['group'])
    save(OUT / f'{dataset}.index.json', {'created_at': stamp(), 'dataset': dataset, 'rows': rows})
    return rows


def probe(path):
    value = json.loads(subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_format',
                      '-show_streams', '-of', 'json', str(path)]))
    stream = next(s for s in value['streams'] if s['codec_type'] == 'audio')
    return float(value['format']['duration']), min(2, int(stream['channels']))


def pcm(path, start, rate, channels, target):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                    '-ss', f'{start:.6f}', '-i', str(path), '-t', '10', '-ar', str(rate),
                    '-ac', str(channels), '-f', 'f32le', '-y', str(target)], check=True, timeout=45)
    if target.stat().st_size != rate * channels * 10 * 4:
        raise ValueError('Incomplete ten-second excerpt')


def extract(dataset, *, limit=None, native=False, sample_fraction=.5, output_dataset=None):
    if not 0 <= sample_fraction <= 1:
        raise ValueError('Excerpt position must lie between the recording start and end')
    OUT.mkdir(parents=True, exist_ok=True)
    rows = prepare(dataset)
    dataset = output_dataset or dataset
    work = OUT / 'pcm'
    work.mkdir(exist_ok=True)
    cache = OUT / 'features'
    cache.mkdir(exist_ok=True)
    provenance = {name: digest(LAB / name) for name in ['model_worker.js', 'audio_attributes.js',
                  'features.js', 'embedding_graph.js', 'runtime_loader.js', 'vendor/bundle.json', 'vendor/runtime.json']}
    provenance['decoder'] = subprocess.check_output(['ffmpeg', '-version'], text=True).splitlines()[0]
    provenance['harness_sha256'] = digest(ROOT / 'experiments/audio_lab_tools/external_audio_worker.mjs')
    provenance['policy'] = POLICY
    provenance['backend'] = 'tensorflow-native' if native else 'tfjs-cpu'
    if native:
        provenance['native_package_sha256'] = digest(LAB / 'local/node/package-lock.json')
    signature = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    save(OUT / 'extractor-provenance.json', {'signature': signature, **provenance})
    environment = dict(os.environ)
    environment.update(EXTERNAL_TFJS_NATIVE='1' if native else '0',
                       TF_NUM_INTRAOP_THREADS='1', TF_NUM_INTEROP_THREADS='1')
    log = (OUT / f'{dataset}.worker.log').open('a')
    worker = subprocess.Popen(['nice', '-n', '10', 'node', str(ROOT / 'experiments/audio_lab_tools/external_audio_worker.mjs')],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                              text=True, bufsize=1, env=environment)
    successes, errors = [], []
    started = time.monotonic()
    try:
        for index, row in enumerate(rows[:limit] if limit else rows):
            try:
                duration, channels = probe(row['audio'])
            except (ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
                errors.append({'id': row['id'], 'error': 'audio_probe_failed: ' + str(error)})
                continue
            if duration < 10:
                errors.append({'id': row['id'], 'error': 'shorter_than_ten_seconds', 'duration': duration})
                continue
            start = (duration - 10) * sample_fraction
            key = hashlib.sha256((row['audio_sha256'] + signature + f'{start:.6f}').encode()).hexdigest()
            feature_path = cache / (key + '.json')
            if feature_path.exists():
                result = json.loads(feature_path.read_text())
            else:
                try:
                    pcm(row['audio'], start, 16000, 1, work / 'model.f32')
                    pcm(row['audio'], start, 44100, channels, work / 'measurement.f32')
                    request = {'id': row['id'], 'model_pcm': str((work / 'model.f32').resolve()),
                               'measurement_pcm': str((work / 'measurement.f32').resolve()), 'channels': channels}
                    worker.stdin.write(json.dumps(request) + '\n')
                    worker.stdin.flush()
                    while True:
                        if not select.select([worker.stdout], [], [], 120)[0]:
                            worker.terminate()
                            raise RuntimeError('Offline worker exceeded 120-second response deadline')
                        line = worker.stdout.readline()
                        if not line:
                            raise RuntimeError('Offline worker exited; inspect worker log')
                        if line.startswith('RESULT '):
                            result = json.loads(line.removeprefix('RESULT '))
                            break
                    if result.get('error'):
                        raise ValueError(result['error'])
                    if len(result['prediction']['embedding']['values']) != 200:
                        raise ValueError('Embedding dimensions changed')
                    if len(result['audio_attributes']['values']) != 27:
                        raise ValueError('Audio attribute dimensions changed')
                    if result.get('research_backend') != ('tensorflow' if native else 'cpu'):
                        raise ValueError('Unexpected inference backend')
                    save(feature_path, result)
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                    errors.append({'id': row['id'], 'error': str(error)})
                    print(json.dumps({'dataset': dataset, 'failed': row['id'], 'error': str(error)[:250]}), flush=True)
                    if worker.poll() is not None:
                        raise RuntimeError('Extraction worker stopped; restart command resumes cached successes') from error
                    continue
            successes.append({**row, 'feature_file': str(feature_path), 'sample_start': start,
                              'sample_seconds': 10, 'recording_duration': duration,
                              'extractor_signature': signature})
            if (index + 1) % 10 == 0 or index == 0:
                progress = {'dataset': dataset, 'completed': len(successes), 'total': len(rows),
                            'errors': len(errors), 'elapsed_seconds': round(time.monotonic() - started, 1),
                            'updated_at': stamp()}
                save(OUT / f'{dataset}.status.json', progress)
                save(OUT / f'{dataset}.features.json', {'rows': successes, 'errors': errors, 'provenance': provenance})
                print(json.dumps(progress), flush=True)
        save(OUT / f'{dataset}.features.json', {'rows': successes, 'errors': errors, 'provenance': provenance})
        save(OUT / f'{dataset}.status.json', {'dataset': dataset, 'completed': len(successes),
             'total': len(rows), 'errors': len(errors), 'elapsed_seconds': round(time.monotonic() - started, 1),
             'updated_at': stamp(), 'state': 'complete' if not limit else 'limited_run'})
    finally:
        try:
            worker.stdin.close()
        except BrokenPipeError:
            pass
        try:
            worker.wait(timeout=15)
        except subprocess.TimeoutExpired:
            worker.terminate()
            worker.wait(timeout=10)
        log.close()
        for file in work.glob('*.f32'):
            file.unlink()
    return successes


def suite(native=False):
    from experiments.audio_lab_tools.external_training import evaluate
    from experiments.audio_lab_tools.external_transfer import evaluate_transfer
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'suite.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({'pid': os.getpid(), 'started_at': stamp()}))
        lock.flush()
        active_model = LAB / 'local/vibe-subgenre-model.json'
        initial_hash = digest(active_model)
        outcomes, completed = [], []
        def checkpoint(state):
            save(OUT / 'suite-status.json', {'updated_at': stamp(), 'pid': os.getpid(),
                 'state': state, 'datasets': outcomes, 'active_model_initial_sha256': initial_hash,
                 'active_model_current_sha256': digest(active_model)})
        checkpoint('running')
        for dataset in ['soundtracks-set2', 'cal500', 'emotify', 'pmemo', 'soundtracks-all']:
            outcome = {'dataset': dataset, 'state': 'extracting', 'started_at': stamp()}
            outcomes.append(outcome)
            checkpoint('running')
            try:
                extract(dataset, native=native)
                outcome['state'] = 'training'
                checkpoint('running')
                report = evaluate(dataset)
                outcome.update(state='complete', rows=report['rows'], finished_at=stamp())
                # Set 1+2 is an alternate Soundtracks experiment, not a second
                # independent training corpus in the accumulated transfer stack.
                if dataset != 'soundtracks-all':
                    completed.append(dataset)
                try:
                    evaluate_transfer(completed)
                    outcome['transfer_state'] = 'complete'
                except Exception as error:
                    outcome['transfer_error'] = repr(error)
            except Exception as error:
                outcome.update(state='failed', error=repr(error), finished_at=stamp())
                print(json.dumps(outcome), flush=True)
            checkpoint('running')
        checkpoint('complete' if all(row['state'] == 'complete' for row in outcomes) else 'complete_with_failures')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'extract', 'evaluate', 'suite'])
    parser.add_argument('--dataset', default='soundtracks-set2', choices=[
        'soundtracks-set2', 'soundtracks-all', 'cal500', 'emotify', 'pmemo', 'vgmidi-original',
        'coimbra-mirex-like', 'deam', 'jamendo-human-00', 'jamendo-human-4shards', '4q-emotion', 'emma-preview-matched'])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps({'dataset': args.dataset, 'rows': len(prepare(args.dataset))}))
    elif args.action == 'extract':
        extract(args.dataset, limit=args.limit, native=args.native)
    elif args.action == 'evaluate':
        from experiments.audio_lab_tools.external_training import evaluate
        evaluate(args.dataset)
    else:
        suite(native=args.native)


if __name__ == '__main__':
    main()
