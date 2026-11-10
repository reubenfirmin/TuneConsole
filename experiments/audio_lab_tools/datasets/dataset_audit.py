"""Audit new audio-backed human annotations before scheduling extraction.

The emitted indices retain native targets and windows. They neither train on
local labels nor treat supplied model predictions as human judgments.
"""
import argparse
from collections import Counter, defaultdict
import csv
from functools import cache
import json
import math
from pathlib import Path
import re
from statistics import mean, pstdev
from urllib.parse import urlparse

from ..support.external_experiment import DATA, digest, save, stamp


def table(path):
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def bounded(value, low, high):
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError('Annotation outside its native range: ' + str(value))
    return result


@cache
def audio_state(path):
    record = path.with_suffix('.json')
    if not path.exists() or not record.exists():
        return {'audio_available': False, 'audio_sha256': None}
    metadata = json.loads(record.read_text())
    actual = digest(path)
    if path.stat().st_size != metadata['archive_member']['bytes'] or actual != metadata['sha256']:
        raise ValueError('Acquired audio differs from its verified record: ' + str(path))
    return {'audio_available': True, 'audio_sha256': actual}


def many_music():
    root = DATA / 'discovery/manymusic'
    base = root / 'metadata/manymusic-stim-zenodo'
    ratings_path = base / 'validation-dataset/ratings/ratings.csv'
    artist_path = base / 'manymusic-dataset/meta/manymusic_mtg_jamendo_tags.csv'
    prompt_path = base / 'validation-dataset/meta/suno-v4.5_effnet-discogs400.csv'
    artists = {row['track_id']: row['artist_id'] for row in table(artist_path)}
    prompts = {row['trackId']: row['prompt_id'] for row in table(prompt_path)}
    members = json.loads((root / 'archive-members.json').read_text())['members']
    available = {Path(row['name']).stem for row in members
                 if row['name'].endswith('.mp3') and '__MACOSX' not in row['name']}
    observations, excluded, source_counts = defaultdict(lambda: defaultdict(list)), defaultdict(int), defaultdict(int)
    for row in table(ratings_path):
        source_counts[row['source']] += 1
        key = row['trackId']
        if key not in available:
            excluded[row['source']] += 1
            continue
        if row['source'] not in ('Jamendo-A', 'Jamendo-B', 'Suno'):
            raise ValueError('Unexpected supplied-audio source')
        start = bounded(row['trackStartSec'], 0, 3600)
        length = bounded(row['trackPlaySec'], 10, 300)
        native = (bounded(row['perceived_arousal'], -1, 1),
                  bounded(row['perceived_valence'], -1, 1))
        # Canonical numbers join spellings such as 0 and 0.0 without rounding
        # away distinct recorded windows. Source A/B are the same audio task.
        observations[(key, start, length)][row['subjId']].append((native, row['source']))
    rows, repeated, conflicting = [], 0, 0
    for (key, start, length), listeners in sorted(observations.items()):
        votes = []
        for responses in listeners.values():
            values = [value for value, _ in responses]
            repeated += len(values) - 1
            conflicting += int(len(set(values)) > 1)
            votes.append([mean(value[index] for value in values) for index in range(2)])
        synthetic = key in prompts
        if not synthetic and key not in artists:
            raise ValueError('Natural recording has no released artist ID: ' + key)
        group = 'manymusic:prompt:' + prompts[key] if synthetic else 'jamendo:artist:' + artists[key]
        path = root / 'rated-audio' / (key + '.mp3')
        rows.append({'id': f'manymusic:{key}:{start}:{length}', 'recording_id': 'manymusic:' + key,
            'audio': str(path), **audio_state(path),
            'group': group, 'artist': '', 'artist_id': artists.get(key), 'prompt_id': prompts.get(key),
            'synthetic_audio': synthetic, 'annotation_start': start, 'annotation_seconds': length,
            'target_names': ['arousal', 'valence'],
            'targets': [mean(vote[i] for vote in votes) for i in range(2)],
            'listener_std': [pstdev(vote[i] for vote in votes) for i in range(2)],
            'rater_count': len(votes), 'listener_ids': sorted(listeners),
            'response_count': sum(map(len, listeners.values())),
            'sources': sorted({source for responses in listeners.values() for _, source in responses}),
            'native_scale': [-1, 1], 'annotation_unit': 'perceived_emotion_of_recorded_30_second_window'})
    report = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'input_sha256': {str(path.relative_to(root)): digest(path) for path in
                        (ratings_path, artist_path, prompt_path, root / 'archive-members.json')},
        'source_response_counts': dict(source_counts), 'excluded_missing_audio_responses': dict(excluded),
        'recordings': len({row['recording_id'] for row in rows}), 'windows': len(rows),
        'natural_recordings': len({row['recording_id'] for row in rows if not row['synthetic_audio']}),
        'synthetic_recordings': len({row['recording_id'] for row in rows if row['synthetic_audio']}),
        'repeated_listener_window_responses': repeated, 'conflicting_listener_windows': conflicting,
        'rows': rows,
        'policy': 'Average repeat responses within a listener and exact window, then equally across listeners. Preserve different windows. Group all windows and A/B occurrences by released artist ID; group synthetic tracks by prompt ID. Report natural and synthetic audio separately. Source-only-selected cross-corpus transfer precedes fitting on this new corpus.',
        'limitations': ['One listener often rates a unique window; a window is not an independent recording.',
            'Jamendo artist IDs do not establish aliases or cross-corpus artist-name equivalence.',
            'Synthetic prompt words, curation votes, liking, familiarity and supplied model outputs are excluded from VA targets.',
            'A center ten-second audio sample would cover only part of a rated thirty-second window; exact coverage must be reported.']}
    save(root / 'human-window-index.json', report)
    print(json.dumps({key: report[key] for key in ('recordings', 'windows', 'natural_recordings',
        'synthetic_recordings', 'repeated_listener_window_responses', 'conflicting_listener_windows')}), flush=True)
    return report


def many_music_credits(text, selected_ids):
    """Join publisher attribution by exact track ID, retaining ambiguous credits."""
    result = {}
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.splitlines()
        match = re.fullmatch(r'\d+/(\d+)\.mp3', lines[0]) if lines else None
        if not match or match[1] not in selected_ids:
            continue
        key = match[1]
        if key in result or len(lines) != 3:
            raise ValueError('Duplicate or malformed selected Jamendo attribution: ' + key)
        attribution = re.fullmatch(r'(.+) from Jamendo: (https?://(?:www\.)?jamendo\.com/track/(\d+))', lines[1])
        description, separator, license_url = lines[2].rpartition(': ')
        location = urlparse(license_url)
        if (not attribution or attribution[3] != key or not separator
                or location.scheme not in ('http', 'https') or not location.netloc):
            raise ValueError('Selected Jamendo provenance has inconsistent identity or URL: ' + key)
        credit = attribution[1]
        unambiguous = credit.count(' by ') == 1
        title, artist = credit.split(' by ') if unambiguous else ('', '')
        result[key] = {'track_id': key, 'source_url': attribution[2],
            'publisher_attribution': lines[1], 'publisher_license_statement': lines[2],
            'license_description': description, 'license_url': license_url,
            'artist': artist, 'title': title, 'artist_credit_unambiguous': unambiguous}
    if set(result) != selected_ids:
        raise ValueError('Missing selected Jamendo attribution: ' + ', '.join(sorted(selected_ids - result.keys())))
    return result


def many_music_provenance():
    root = DATA / 'discovery/manymusic'
    index_path = root / 'human-window-index.json'
    index = json.loads(index_path.read_text())
    rows = {row['recording_id']: row for row in index['rows']}
    natural = {key.removeprefix('manymusic:') for key, row in rows.items() if not row['synthetic_audio']}
    path = root / 'metadata/manymusic-stim-zenodo/manymusic-dataset/meta/audio_licenses.txt'
    acquisition_path = root / 'audio-provenance-acquisition.json'
    acquisition = json.loads(acquisition_path.read_text())
    if (digest(path) != acquisition['sha256'] or path.stat().st_size != acquisition['archive_member']['bytes']
            or acquisition.get('crc32_verified') is not True):
        raise ValueError('Publisher attribution differs from the verified archive member')
    credits = many_music_credits(path.read_text(), natural)
    joined = [{**credits[key], 'recording_id': 'manymusic:' + key,
               'artist_id': rows['manymusic:' + key]['artist_id'],
               'audio_sha256': rows['manymusic:' + key]['audio_sha256']}
              for key in sorted(credits)]
    report = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'human_index_sha256': digest(index_path), 'attribution_sha256': digest(path),
        'acquisition_sha256': digest(acquisition_path), 'rows': joined,
        'license_url_counts': dict(Counter(row['license_url'] for row in joined)),
        'unambiguous_artist_credits': sum(row['artist_credit_unambiguous'] for row in joined),
        'generated_audio_recordings': sum(row['synthetic_audio'] for row in rows.values()),
        'scope': 'Exact publisher track-ID attribution for acquired natural audio. Preserve source terms per recording; these records do not establish rights for generated audio, product use, or upstream model pretraining. Artist strings supplement catalog IDs for conservative overlap exclusions; aliases remain unresolved.'}
    save(root / 'audio-provenance-index.json', report)
    print(json.dumps({key: report[key] for key in ('license_url_counts', 'unambiguous_artist_credits',
                                                 'generated_audio_recordings')}), flush=True)
    return report


def memo():
    root = DATA / 'discovery/memo2496'
    base = root / 'metadata'
    info = table(base / 'songs_info_all.csv')
    if len({row['song_id'] for row in info}) != len(info):
        raise ValueError('Duplicate Memo2496 song ID')
    coordinates = {}
    for name in ('arousal', 'valence'):
        rows = table(base / (name + '_all_average.csv'))
        coordinates[name] = {row['song_id']: row for row in rows}
        if len(coordinates[name]) != len(rows) or coordinates[name].keys() != {row['song_id'] for row in info}:
            raise ValueError('Memo2496 coordinate identities differ')
    members = {row['name'] for row in json.loads((root / 'archive-members.json').read_text())['members']}
    selected = {row['name'] for row in json.loads((root / 'pilot-selection.json').read_text())['members']}
    rows = []
    for item in info:
        if item['file_name'] not in members:
            raise ValueError('Missing supplied Memo2496 audio')
        if item['file_name'] not in selected:
            continue
        series = {}
        for name, values in coordinates.items():
            series[name] = [[int(key.removeprefix('sample_').removesuffix('ms')) / 1000,
                             bounded(value, -1, 1)] for key, value in values[item['song_id']].items()
                            if key.startswith('sample_') and value is not None and value.strip() and value.lower() != 'nan']
        if not all(series.values()):
            raise ValueError('Missing Memo2496 pilot annotations')
        path = root / 'pilot-audio' / item['file_name']
        rows.append({'id': 'memo2496:' + item['song_id'], 'audio': str(path), **audio_state(path),
            'group': 'memo2496:' + item['song_id'], 'artist': '', 'artist_identity_unknown': True,
            'platform': item['platform'], 'native_scale': [-1, 1], 'coordinates': series,
            'policy': 'Match supplied song_id to UUID filename and timestamped expert averages. Compare real covered windows; exclude padded audio and unlabeled time. Do not use the emotion field as a human categorical target.'})
    inputs = [base / name for name in ('songs_info_all.csv', 'arousal_all_average.csv', 'valence_all_average.csv')]
    report = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'input_sha256': {str(path.relative_to(root)): digest(path) for path in inputs},
        'selection_sha256': digest(root / 'pilot-selection.json'), 'rows': rows,
        'limitations': ['Pilot selected by filename hash before labels, not a complete 2496-track benchmark.',
            'Authors removed artist metadata. Recording separation cannot demonstrate artist independence.',
            'The native release uses 15–75-second segment averages; shorter aligned checks are a different task.',
            'Emotion anchor words and radial distance do not establish the product six-vibe meanings or shared intensity.']}
    save(root / 'pilot-human-index.json', report)
    print(json.dumps({'memo_pilot_annotation_joins': len(rows)}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', choices=['manymusic', 'manymusic-provenance', 'memo2496'])
    args = parser.parse_args()
    {'manymusic': many_music, 'manymusic-provenance': many_music_provenance,
     'memo2496': memo}[args.dataset]()
