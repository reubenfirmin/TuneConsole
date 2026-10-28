"""Verified joins for newly recovered external audio; research files only."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import subprocess
import tarfile

from .external_experiment import DATA, OUT, digest, normalize, save, stamp, table

JAMENDO = DATA / 'mtg-jamendo'
JAMENDO_SOURCE = JAMENDO / 'mtg-jamendo-dataset-cafd8e20c265ed84f1e61f1c875327971f43a62f'
JAMENDO_TASKS = ('acoustic', 'aggressive', 'electronic', 'happy', 'party', 'relaxed', 'sad')
COIMBRA_PREFIX = 'Audio and Lyric Sentiment Neural Network Regression/MIREX-like_mood'
REPOSITORIES = {
    'coimbra-mirex-like': ('https://github.com/ethanray123/AudioEmotionPredictionModel',
                         '8bdd6492f520d76cc2fa74ff69885b2b2b9c5777',
                         [COIMBRA_PREFIX + '/README.txt', COIMBRA_PREFIX + '/dataset/Audio',
                          COIMBRA_PREFIX + '/dataset/clusters.txt', COIMBRA_PREFIX + '/dataset/categories.txt',
                          COIMBRA_PREFIX + '/dataset/dataset info.csv']),
    'deam': ('https://github.com/Akminar/Classifying-Emotionality-of-Music',
             '3db39c68bf69609b57cdc31dd46fa9f8fd24970f', ['data/MEMD_audio', 'README.md']),
    '4q-emotion': ('https://github.com/ritam413/Music-Mood-Classifier-',
                   '3b29a3f47c669b7a0c992211d638641ca5079594', ['data/archive/MER Audio', 'README.md']),
}


def checkout(dataset):
    repository, expected, paths = REPOSITORIES[dataset]
    base = DATA / dataset
    source, git = base / 'source', base / 'source.git'
    revision = subprocess.check_output(['git', '--git-dir=' + str(git.resolve()),
                                        'rev-parse', 'FETCH_HEAD'], text=True).strip()
    if revision != expected:
        raise ValueError(f'Unexpected {dataset} source revision: {revision}')
    source.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', '--git-dir=' + str(git.resolve()), '--work-tree=' + str(source.resolve()),
                    'checkout', 'FETCH_HEAD', '--', *paths], check=True, timeout=300)
    audio = sorted(source.rglob('*.mp3'))
    save(base / 'audio-acquisition.json', {'created_at': stamp(), 'repository': repository,
         'commit': revision, 'selected_paths': paths, 'audio_files': len(audio),
         'audio_bytes': sum(path.stat().st_size for path in audio),
         'policy': 'Audio mirror only; original native annotations and exact identifiers determine targets.'})


def jamendo_annotations():
    path = JAMENDO_SOURCE / 'derived/music-classification-annotations/music-classification-annotations-raw.tsv'
    rows = []
    with path.open() as stream:
        next(stream)
        for line in stream:
            fields = line.rstrip('\n').split('\t')
            annotations = dict(item.split('---', 1) for item in fields[5:])
            values, counts = [], []
            for name in JAMENDO_TASKS:
                votes = annotations.get('mood_' + name, '').split(',')
                if not votes or any(vote not in (name, 'not_' + name) for vote in votes):
                    break
                values.append(votes.count(name) / len(votes))
                counts.append(len(votes))
            else:
                rows.append({'id': fields[0], 'artist': fields[1], 'album': fields[2],
                             'path': fields[3], 'duration': float(fields[4]),
                             'targets': values, 'rater_counts': counts})
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate Jamendo human annotation identifier')
    return rows


def checksums(path):
    return {name.lstrip('*'): value for value, name in
            (line.strip().split(maxsplit=1) for line in path.read_text().splitlines() if line.strip())}


def stage_jamendo(shards=('00',), remove_archive=False):
    """Verify complete official tars; retain only human-annotated mood recordings."""
    download = JAMENDO_SOURCE / 'data/download'
    tar_hashes = checksums(download / 'autotagging_moodtheme_audio-low_sha256_tars.txt')
    track_hashes = checksums(download / 'autotagging_moodtheme_audio-low_sha256_tracks.txt')
    wanted = {row['path'].replace('.mp3', '.low.mp3'): row for row in jamendo_annotations()}
    target = JAMENDO / 'human-audio'
    target.mkdir(exist_ok=True)
    for shard in shards:
        filename = f'autotagging_moodtheme_audio-low-{shard}.tar'
        archive = JAMENDO / 'audio-low' / filename
        if not archive.exists():
            archive = archive.with_suffix('.tar.part')
        expected_names = {name for name in wanted if name.startswith(shard + '/')}
        selected = []
        if not archive.exists():
            manifest_path = JAMENDO / f'human-shard-{shard}.json'
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                if manifest['archive_sha256'] == tar_hashes[filename] and all(
                    (target / name).exists() and digest(target / name) == value
                    for name, value in manifest['track_sha256'].items()
                ) and manifest['verified_tracks']:
                    continue
            raise FileNotFoundError(archive)
        if digest(archive) != tar_hashes[filename]:
            raise ValueError('Incomplete or invalid Jamendo tar: ' + filename)
        with tarfile.open(archive) as source:
            for member in source:
                if member.name not in expected_names:
                    continue
                if not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise ValueError('Unsafe Jamendo archive member')
                destination = target / member.name
                destination.parent.mkdir(exist_ok=True)
                with source.extractfile(member) as incoming, destination.open('wb') as outgoing:
                    import shutil
                    shutil.copyfileobj(incoming, outgoing)
                if digest(destination) != track_hashes[member.name]:
                    raise ValueError('Jamendo recording checksum mismatch: ' + member.name)
                selected.append(member.name)
        # Only moodtheme audio is in these tars; other human-labelled tracks are absent.
        save(JAMENDO / f'human-shard-{shard}.json', {'created_at': stamp(), 'archive': filename,
             'archive_sha256': tar_hashes[filename], 'verified_tracks': sorted(selected),
             'track_sha256': {name: track_hashes[name] for name in selected},
             'url': 'https://cdn.freesound.org/mtg-jamendo/autotagging_moodtheme/audio-low/' + filename,
             'selection': 'Exact human-annotation PATH intersection with supplied moodtheme audio; no predictions or developer vibe labels used.'})
        if remove_archive:
            archive.unlink()


def jamendo_rows(shards=('00',)):
    rows = []
    for item in jamendo_annotations():
        path = item['path'].replace('.mp3', '.low.mp3')
        if path.split('/')[0] not in shards:
            continue
        audio = JAMENDO / 'human-audio' / path
        if not audio.exists():
            continue
        rows.append({'id': item['id'], 'audio': str(audio), 'group': 'jamendo:' + item['artist'],
                     'artist': '', 'title': '', 'source_artist_id': item['artist'],
                     'source_album_id': item['album'], 'source_path': item['path'],
                     'target_names': list(JAMENDO_TASKS), 'targets': item['targets'],
                     'rater_counts': item['rater_counts'], 'native_scale': [0, 1],
                     'annotation_unit': 'whole_song_explicit_human_binary_vote_fraction',
                     'annotation_duration': item['duration']})
    save(OUT / ('jamendo-human-00' if shards == ('00',) else 'jamendo-human-4shards') / 'join-audit.json',
         {'created_at': stamp(), 'rows': len(rows), 'artists': len({row['group'] for row in rows}),
          'shards': list(shards), 'targets': list(JAMENDO_TASKS),
          'annotation_sha256': digest(JAMENDO_SOURCE / 'derived/music-classification-annotations/music-classification-annotations-raw.tsv'),
          'policy': 'All seven explicit positive/negative human tasks present; target is positive-vote fraction. Missing uploader tags never become negative labels.',
          'limitations': ['These annotations originate from Jamendo split-0 test; that original test split is no longer an untouched holdout.',
                         'Artist identifiers group validation, but artist names are unavailable for cross-corpus gold identity exclusion.']})
    return rows


def coimbra_rows():
    base = DATA / 'coimbra-mirex-like/source' / COIMBRA_PREFIX / 'dataset'
    metadata = base / 'dataset info.csv'
    encoding = 'utf-16' if metadata.read_bytes().startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    with metadata.open(encoding=encoding, newline='') as stream:
        raw = list(csv.reader(stream, delimiter=';'))
    header, identities, repairs = raw[0], [], []
    for number, fields in enumerate(raw[1:], 1):
        # The release has one unquoted semicolon in this title. Require its
        # exact numbered identity before repairing the known structural defect.
        if len(fields) == len(header) + 1 and fields[-2] == '782.mp3':
            repairs.append({'line': number + 1, 'filename': fields[-2],
                            'policy': 'Merge the two unquoted title fragments with the original semicolon.'})
            fields = [fields[0] + ';' + fields[1], *fields[2:]]
        if len(fields) != len(header):
            raise ValueError(f'Unexpected Coimbra CSV width on line {number + 1}')
        identities.append(dict(zip(header, fields)))
    clusters = (base / 'clusters.txt').read_text().splitlines()
    if len(identities) != 903 or len(clusters) != len(identities):
        raise ValueError('Coimbra metadata/cluster alignment mismatch')
    rows = []
    for item, cluster in zip(identities, clusters):
        number = int(cluster.strip().removeprefix('Cluster '))
        if number not in range(1, 6):
            raise ValueError('Unexpected Coimbra cluster')
        filename = item['Filename'].strip()
        path = base / 'Audio' / filename
        if not path.exists() or Path(filename).name != filename:
            raise ValueError('Missing or unsafe Coimbra recording identity: ' + filename)
        artist, title = item['Artist'].strip(), item['Title'].strip()
        rows.append({'id': 'coimbra:' + path.stem, 'audio': str(path),
                     'group': 'coimbra:' + (normalize(artist) or path.stem), 'artist': artist, 'title': title,
                     'target_names': ['rousing', 'cheerful', 'brooding', 'quirky', 'aggressive'],
                     'targets': [int(number == index) for index in range(1, 6)], 'native_scale': [0, 1],
                     'annotation_unit': 'whole_song_allmusic_tag_derived_mirex_like_cluster'})
    save(OUT / 'coimbra-mirex-like.join-audit.json', {'created_at': stamp(), 'rows': len(rows),
         'artists': len({row['group'] for row in rows}), 'cluster_counts': dict(Counter(clusters)),
         'metadata_sha256': digest(base / 'dataset info.csv'), 'clusters_sha256': digest(base / 'clusters.txt'),
         'metadata_repairs': repairs,
         'policy': 'Original row-aligned cluster file and exact numbered filename; distinct from original MIREX and 4Q datasets.',
         'target_provenance': 'AllMusic mood tags determine the most represented MIREX-like cluster; not newly collected listener ratings.'})
    return rows


def deam_rows():
    base = DATA / 'deam'
    annotations = base / 'annotations/annotations averaged per song/song_level'
    identity_path = base / 'audio-identities.json'
    identities = json.loads(identity_path.read_text())['recordings'] if identity_path.exists() else {}
    rows = []
    for path in sorted(annotations.glob('static_annotations_averaged_songs_*.csv')):
        for item in table(path):
            identifier = str(int(item['song_id']))
            audio = base / 'source/data/MEMD_audio' / (identifier + '.mp3')
            if not audio.exists():
                raise ValueError('Missing DEAM audio identifier ' + identifier)
            identity = identities.get(identifier, {})
            artist, title = identity.get('artist', ''), identity.get('title', '')
            rows.append({'id': 'deam:' + identifier, 'audio': str(audio),
                         'group': 'deam:artist:' + normalize(artist) if normalize(artist) else 'deam:' + identifier,
                         'artist': artist, 'title': title, 'target_names': ['valence', 'arousal'],
                         'targets': [(float(item[name]) - 1) / 8 for name in ('valence_mean', 'arousal_mean')],
                         'native_scale': [1, 9], 'annotation_unit': 'supplied_audio_static_listener_mean',
                         'annotation_source': str(path),
                         'native_standard_deviations': [float(item[name]) for name in ('valence_std', 'arousal_std')]})
    if len(rows) != 1802:
        raise ValueError('Expected 1802 DEAM annotation/audio pairs')
    save(OUT / 'deam.join-audit.json', {'created_at': stamp(), 'rows': len(rows),
         'artist_metadata_rows': sum(bool(row['artist']) for row in rows),
         'annotation_sha256': {path.name: digest(path) for path in annotations.glob('*.csv')},
         'policy': 'Exact original numeric identifier joins supplied MP3 with original averaged static annotations; native [1,9] scale maps to [0,1].',
         'limitations': ['Artist/song-title metadata is incomplete; recording grouping is used where artist tags are unavailable.',
                        'No claim of complete unseen-artist validation or complete cross-corpus overlap removal.']})
    return rows


def deam_metadata():
    base = DATA / 'deam'
    recordings = {}
    for path in sorted((base / 'source/data/MEMD_audio').glob('*.mp3')):
        value = json.loads(subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_format',
                                                   '-of', 'json', str(path)]))['format']
        tags = {key.lower(): item for key, item in value.get('tags', {}).items()}
        recordings[path.stem] = {'artist': tags.get('artist', '').strip(),
                                 'title': tags.get('title', '').strip(),
                                 'duration': float(value['duration'])}
    save(base / 'audio-identities.json', {'created_at': stamp(), 'recordings': recordings,
         'policy': 'Literal supplied MP3 format tags; no inferred identities or feature/label matching.',
         'artist_coverage': sum(bool(row['artist']) for row in recordings.values()),
         'title_coverage': sum(bool(row['title']) for row in recordings.values())})


def quadrant_rows():
    base = DATA / '4q-emotion/source/data/archive/MER Audio'
    metadata = {row['song']: row for row in table(base / 'panda_dataset_taffc_metadata.csv', encoding='cp1252')}
    rows, mismatches = [], []
    for quadrant in range(1, 5):
        paths = sorted((base / f'Q{quadrant}').glob('*.mp3'))
        if len(paths) != 225:
            raise ValueError('4Q quadrant does not contain 225 supplied clips')
        for path in paths:
            item = metadata[path.stem]
            if item['quadrant'] != f'Q{quadrant}':
                mismatches.append({'id': path.stem, 'final_audio_folder': f'Q{quadrant}',
                                   'metadata_automatic_quadrant': item['quadrant']})
            rows.append({'id': '4q:' + path.stem, 'audio': str(path),
                         'group': '4q:' + (normalize(item['artist']) or path.stem),
                         'artist': item['artist'], 'title': item['title'],
                         'target_names': ['high_positive', 'high_negative', 'low_negative', 'low_positive'],
                         'targets': [int(quadrant == index) for index in range(1, 5)],
                         'annotation_unit': 'supplied_30_second_final_quadrant', 'native_scale': [0, 1]})
    save(OUT / '4q-emotion.join-audit.json', {'created_at': stamp(), 'rows': len(rows),
         'artists': len({row['group'] for row in rows}), 'quadrant_counts': [225] * 4,
         'metadata_sha256': digest(base / 'panda_dataset_taffc_metadata.csv'),
         'folder_metadata_disagreements': mismatches,
         'policy': 'Exact AllMusic recording identifier joins metadata; supplied final quadrant folder is the native target, not a freshly inferred VA label.'})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['checkout', 'stage-jamendo', 'deam-metadata'])
    parser.add_argument('--dataset', choices=list(REPOSITORIES))
    parser.add_argument('--shards', nargs='+', default=['00'])
    parser.add_argument('--remove-archive', action='store_true')
    args = parser.parse_args()
    if args.action == 'checkout':
        checkout(args.dataset)
    elif args.action == 'deam-metadata':
        deam_metadata()
    else:
        stage_jamendo(tuple(args.shards), args.remove_archive)


if __name__ == '__main__':
    main()
