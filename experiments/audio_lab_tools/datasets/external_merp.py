"""Acquire the pinned author MERP release without collecting rater profiles."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import PurePosixPath
import subprocess
import time
from urllib.request import urlopen

from ..support.external_experiment import DATA, OUT, digest, save, stamp
from .external_secondary import identity

REVISION = '04d90b60d92f079f2e25759e48f233094f7af84a'
BASE = DATA / 'merp'
URL = 'https://huggingface.co/datasets/amaai-lab/MERP/resolve/' + REVISION + '/'


def acquire():
    tree = json.loads((BASE / 'release-tree.json').read_text())
    selected = [row for row in tree if row['type'] == 'file' and
                (row['path'].startswith('audio/') or row['path'] in
                 ('README.md', 'annotations_filtered.parquet', 'annotations_raw.parquet'))]
    if sum(row['path'].endswith('.wav') for row in selected) != 54:
        raise ValueError('Pinned MERP release must supply 54 WAV files')

    def download(row):
        relative = PurePosixPath(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe release path')
        path = BASE / relative
        path.parent.mkdir(parents=True, exist_ok=True)

        def verified(candidate):
            if not candidate.exists() or candidate.stat().st_size != row['size']:
                return False
            if row.get('lfs'):
                return digest(candidate) == row['lfs']['oid']
            content = candidate.read_bytes()
            return hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest() == row['oid']

        if not verified(path):
            partial = path.with_suffix(path.suffix + '.partial')
            for attempt in range(3):
                try:
                    with urlopen(URL + str(relative), timeout=90) as source, partial.open('wb') as target:
                        while block := source.read(1024 * 1024):
                            target.write(block)
                    if not verified(partial):
                        raise ValueError('MERP file failed pinned size/hash verification: ' + str(relative))
                    partial.replace(path)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt)
        return str(relative), {'bytes': path.stat().st_size, 'sha256': digest(path)}

    files = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for pending in as_completed([pool.submit(download, row) for row in selected]):
            filename, record = pending.result()
            files[filename] = record
            print(json.dumps({'merp_acquired': len(files), 'total': len(selected), 'file': filename}), flush=True)
    report = {'created_at': stamp(), 'source': 'https://huggingface.co/datasets/amaai-lab/MERP',
              'revision': REVISION, 'tree_sha256': digest(BASE / 'release-tree.json'), 'files': files,
              'policy': 'Pinned author release; verify Git LFS SHA256 or Git blob SHA1 and byte count. '
                        'Do not acquire demographic profiles. Four DEAM anchors are not independent evaluation audio.'}
    save(BASE / 'acquisition.json', report)
    return report


def inspect_annotations():
    # This optional parser is installed only under ignored experiment files.
    import sys
    sys.path.insert(0, str((DATA.parent / 'python-deps').resolve()))
    import pyarrow
    import pyarrow.parquet as parquet
    import numpy as np
    from collections import Counter, defaultdict

    rows = parquet.read_table(BASE / 'annotations_filtered.parquet').to_pylist()
    pairs, repeats, lengths, outside = set(), [], defaultdict(list), 0
    for row in rows:
        pair = (row['song_id'], row['rater_id'])
        if pair in pairs:
            repeats.append(pair)
        pairs.add(pair)
        for name in ('valence', 'arousal'):
            values = np.asarray(row[name], dtype=float)
            if not len(values) or not np.isfinite(values).all():
                raise ValueError('Missing/nonfinite MERP sequence')
            lengths[row['song_id']].append(len(values))
            outside += int(((values < -1) | (values > 1)).sum())
    report = {'created_at': stamp(), 'rows': len(rows),
              'tracks': len(lengths), 'raters': len({row['rater_id'] for row in rows}),
              'repeated_song_rater_pairs': repeats, 'samples_outside_declared_scale': outside,
              'sequence_lengths_by_song': {name: dict(Counter(values)) for name, values in lengths.items()},
              'annotation_sha256': digest(BASE / 'annotations_filtered.parquet'),
              'pyarrow_version': pyarrow.__version__, 'script_sha256': digest(__file__),
              'limitations': ['README claims 10 Hz; establish actual time units against supplied audio before alignment.',
                             'Per-rater rescaling and smoothing can exceed the nominal scale.',
                             'Quality filtering uses four DEAM anchors; exclude anchors as independent source evidence.',
                             'Artist metadata and cross-corpus recording overlap still require verification.']}
    save(OUT / 'merp.annotation-audit.json', report)
    print(json.dumps({key: report[key] for key in ('rows', 'tracks', 'raters',
          'repeated_song_rater_pairs', 'samples_outside_declared_scale')}), flush=True)
    return report


def prepare_rows(*, lifetime_fd=None):
    """Use average native dynamic judgments as explicitly weak static targets."""
    from collections import defaultdict
    import sys
    sys.path.insert(0, str((DATA.parent / 'python-deps').resolve()))
    import numpy as np
    import pyarrow.parquet as parquet

    acquisition_path = BASE / 'acquisition.json'
    acquisition = json.loads(acquisition_path.read_text())
    if acquisition['revision'] != REVISION:
        raise ValueError('Unexpected MERP source revision')
    annotations = BASE / 'annotations_filtered.parquet'
    if digest(annotations) != acquisition['files']['annotations_filtered.parquet']['sha256']:
        raise ValueError('MERP annotations changed after acquisition')
    supplied = parquet.read_table(annotations).to_pylist()
    tracks = defaultdict(lambda: defaultdict(list))
    anchors = set()
    for row in supplied:
        song = row['song_id']
        if song.startswith('deam_'):
            anchors.add(song)
            continue
        if PurePosixPath(song).name != song or '/' in song or '..' in song:
            raise ValueError('Invalid MERP song identity')
        values = []
        for name in ('valence', 'arousal'):
            sequence = np.asarray(row[name], dtype=float)
            if not len(sequence) or not np.isfinite(sequence).all() or (np.abs(sequence) > 1).any():
                raise ValueError('Invalid MERP native rating sequence')
            values.append(float(sequence.mean()))
        tracks[song][row['rater_id']].append(values)
    if len(anchors) != 4 or len(tracks) != 50:
        raise ValueError('Expected 50 FMA songs and four excluded DEAM anchors')
    rows, metadata, repeated = [], {}, []
    for song, listeners in sorted(tracks.items()):
        filename = 'audio/' + song + '.wav'
        audio = BASE / filename
        if digest(audio) != acquisition['files'][filename]['sha256']:
            raise ValueError('MERP audio changed: ' + filename)
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_entries',
            'format=duration:format_tags', '-of', 'json', str(audio)], text=True,
            pass_fds=() if lifetime_fd is None else (lifetime_fd,)))['format']
        tags = {key.casefold(): value for key, value in info.get('tags', {}).items()}
        artist, title = tags.get('artist', '').strip(), tags.get('title', '').strip()
        metadata[song] = {'duration': float(info['duration']), 'tags': tags}
        listener_means = []
        for listener, trials in listeners.items():
            if len(trials) > 1:
                repeated.append({'song': song, 'listener': listener, 'trials': len(trials)})
            listener_means.append(np.mean(trials, axis=0))
        targets = (np.mean(listener_means, axis=0) + 1) / 2
        rows.append({'id': 'merp:' + song, 'audio': str(audio), 'artist': artist, 'title': title,
            'group': 'merp:artist:' + identity(artist) if identity(artist) else 'merp:' + song,
            'target_names': ['valence', 'arousal'], 'targets': targets.tolist(), 'native_scale': [-1, 1],
            'annotation_unit': 'filtered_dynamic_listener_means_weak_static_target_for_center_excerpt',
            'rater_count': len(listeners), 'source_family': 'merp',
            'audio_copyright_tag': tags.get('copyright', '')})
    audit = {'created_at': stamp(), 'revision': REVISION, 'rows': len(rows),
        'artist_groups': len({row['group'] for row in rows}),
        'missing_artists': [row['id'] for row in rows if not row['artist']],
        'excluded_deam_anchors': sorted(anchors), 'repeated_listener_trials': repeated,
        'audio_metadata': metadata, 'annotation_sha256': digest(annotations),
        'acquisition_sha256': digest(acquisition_path), 'script_sha256': digest(__file__),
        'policy': 'Average each native filtered dynamic series; average repeated trials within listener/song, '
                  'then average listeners equally. Normalize [-1,1] to [0,1]. Exclude every DEAM anchor.',
        'limitations': ['Native targets average the available filtered timeline; they are not direct static ratings.',
                       'Center ten-second audio may not represent emotional changes elsewhere in the timeline.',
                       'Filtered sequences correspond to author resampling to 0.5-second steps and head removal, '
                       'despite the release README describing the original 10 Hz rate. Exact alignment remains unverified.',
                       'This small, emotionally selected corpus is weak auxiliary evidence, with per-track audio terms.']}
    save(OUT / 'merp-static.join-audit.json', audit)
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('acquire', 'inspect', 'prepare'))
    options = parser.parse_args()
    {'acquire': acquire, 'inspect': inspect_annotations, 'prepare': prepare_rows}[options.action]()
