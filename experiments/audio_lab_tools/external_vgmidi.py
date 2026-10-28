"""Acquire the author's rendered VGMIDI audio and queue its native-label trial."""
from collections import defaultdict
import json
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np

from .external_experiment import DATA, OUT, digest, normalize, probe, save, stamp

BASE = DATA / 'vgmidi'
SOURCE = BASE / 'source'
GIT = BASE / 'source.git'


def unpack_source():
    started = time.monotonic()
    while True:
        head = GIT / 'FETCH_HEAD'
        if head.exists() and head.stat().st_size > 40:
            break
        if time.monotonic() - started > 2400:
            raise RuntimeError('Author repository fetch did not complete within forty minutes')
        time.sleep(15)
    if shutil.disk_usage(BASE).free < 4 * 1024 ** 3:
        raise RuntimeError('Need four GB free before checking out source WAV recordings')
    SOURCE.mkdir(exist_ok=True)
    subprocess.run(['git', '--git-dir=' + str(GIT.resolve()), '--work-tree=' + str(SOURCE.resolve()),
                    'checkout', 'FETCH_HEAD', '--', 'labelled/audio', 'labelled/annotations', 'README.md',
                    'vgmidi_labelled.csv'], check=True, timeout=180)
    revision = subprocess.check_output(['git', '--git-dir=' + str(GIT.resolve()), 'rev-parse', 'FETCH_HEAD'], text=True).strip()
    save(BASE / 'audio-acquisition.json', {'created_at': stamp(), 'repository': 'https://github.com/lucasnfe/vgmidi',
         'commit': revision, 'audio_files': len(list((SOURCE / 'labelled/audio').glob('*.wav'))),
         'annotation_sha256': {p.name: digest(p) for p in (SOURCE / 'labelled/annotations').glob('*.json')},
         'stimulus_policy': 'Author-supplied rendered WAV recordings; no new MIDI rendering and no commercial-game audio substitutions.'})


def prepare_rows():
    paths = sorted((SOURCE / 'labelled/audio').glob('*.wav'))
    if not paths:
        raise ValueError('No author-rendered VGMIDI recordings available')
    rows, excluded = [], []
    for annotation_path in sorted((SOURCE / 'labelled/annotations').glob('vgmidi_raw_*.json')):
        data = json.loads(annotation_path.read_text())
        annotations = defaultdict(list)
        for key, value in data['annotations'].items():
            annotations[key.rsplit('_', 1)[0]].append(value)
        for key, identity in data['pieces'].items():
            title = normalize(Path(identity['audio']).stem)
            series = identity['midi'].split('_')[0]
            normalized_series = normalize(series.removeprefix('The '))
            candidates = [p for p in paths if normalize(p.stem).endswith(title)
                          and normalized_series in normalize(p.stem)]
            identifier = f'vgmidi:{annotation_path.stem}:{key}'
            if len(candidates) != 1:
                excluded.append({'id': identifier, 'title': identity['name'],
                                 'reason': 'no_unique_title_and_franchise_match', 'candidates': [p.name for p in candidates]})
                continue
            path = candidates[0]
            duration, _ = probe(path)
            if abs(duration - identity['duration']) > 2:
                excluded.append({'id': identifier, 'reason': 'duration_mismatch',
                                 'audio_duration': duration, 'annotation_duration': identity['duration']})
                continue
            ratings = []
            for rating in annotations[key]:
                a, v = np.asarray(rating['arousal'], dtype=float), np.asarray(rating['valence'], dtype=float)
                if len(a) != identity['measures'] or len(v) != identity['measures']:
                    continue
                if not np.isfinite([a, v]).all() or np.abs([a, v]).max() > 1:
                    continue
                ratings.append([a.mean(), v.mean()])
            if len(ratings) < 5:
                excluded.append({'id': identifier, 'reason': 'too_few_complete_ratings', 'usable_raters': len(ratings)})
                continue
            rows.append({'id': identifier, 'audio': str(path), 'group': 'vgmidi:franchise:' + normalize(series),
                         'artist': '', 'title': identity['name'], 'franchise': series,
                         'target_names': ['arousal', 'valence'],
                         'targets': ((np.mean(ratings, axis=0) + 1) / 2).tolist(),
                         'rater_count': len(ratings), 'native_scale': [-1, 1],
                         'annotation_unit': 'whole_piano_arrangement_weak_label',
                         'annotation_source': str(annotation_path), 'annotation_piece_id': key,
                         'source_midi_name': identity['midi'], 'annotation_duration': identity['duration']})
    if len({row['audio'] for row in rows}) != len(rows):
        raise ValueError('Repeated VGMIDI recording identity needs explicit grouping')
    save(OUT / 'vgmidi-original.join-audit.json', {'created_at': stamp(), 'available_wavs': len(paths),
         'matched_rows': len(rows), 'franchises': sorted({row['franchise'] for row in rows}), 'excluded': excluded,
         'policy': 'Unique normalized annotation-audio title suffix plus franchise; duration within two seconds; at least five complete listeners.',
         'target_policy': 'Equal-listener mean of each complete bar-by-bar annotation, normalized from [-1,1]. Center audio is a weak sample of this whole-arrangement target.',
         'audio_acquisition_sha256': digest(BASE / 'audio-acquisition.json')})
    if len(rows) < 30 or len({row['group'] for row in rows}) < 5:
        raise ValueError('Too few verified recordings or franchises for grouped validation')
    return rows


def wait_for_predecessors():
    while True:
        native = json.loads((OUT / 'suite-status.json').read_text())['state']
        path = OUT / 'unlabeled-consistency/status.json'
        consistency = json.loads(path.read_text())['state'] if path.exists() else 'not_started'
        if native.startswith('complete') and consistency in ('complete', 'failed'):
            return
        time.sleep(15)


def main():
    status_path = OUT / 'additional-datasets-status.json'
    try:
        save(status_path, {'state': 'acquiring', 'dataset': 'vgmidi-original', 'updated_at': stamp()})
        unpack_source()
        rows = prepare_rows()
        save(status_path, {'state': 'queued_after_consistency', 'dataset': 'vgmidi-original',
                          'verified_rows': len(rows), 'updated_at': stamp()})
        wait_for_predecessors()
        from .external_experiment import extract
        from .external_training import evaluate
        from .external_transfer import evaluate_transfer
        from .external_consensus import evaluate as consensus
        save(status_path, {'state': 'extracting', 'dataset': 'vgmidi-original', 'updated_at': stamp()})
        extract('vgmidi-original', native=True)
        report = evaluate('vgmidi-original')
        report['limitations'].extend(['Franchise groups separate related game music; composer identities are unavailable.',
                                     'This is the supplied-audio subset, not the complete 200-piece collection.',
                                     'Targets average complete time-varying listener annotations; one center excerpt is weak supervision.'])
        save(OUT / 'vgmidi-original.report.json', report)
        native = json.loads((OUT / 'suite-status.json').read_text())
        datasets = [row['dataset'] for row in native['datasets'] if row['state'] == 'complete'
                    and row['dataset'] != 'soundtracks-all'] + ['vgmidi-original']
        evaluate_transfer(datasets)
        consensus(datasets)
        save(status_path, {'state': 'complete', 'dataset': 'vgmidi-original', 'rows': report['rows'], 'updated_at': stamp()})
    except Exception as error:
        save(status_path, {'state': 'failed', 'dataset': 'vgmidi-original', 'error': repr(error), 'updated_at': stamp()})
        raise


if __name__ == '__main__':
    main()
