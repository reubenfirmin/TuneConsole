"""Strict EMMA annotation joins for the publisher's current audio previews."""
import argparse
from collections import Counter
import csv
import json
import re
from urllib.parse import quote

from .external_experiment import DATA, OUT, digest, save, stamp
from .external_secondary import artist_keys, identity

BASE = DATA / 'emma'
TARGETS = ('Wonder', 'Transcendence', 'Nostalgia', 'Tenderness', 'Peacefulness',
           'Joy', 'Power', 'Tension', 'Sadness')


def joined():
    original_path = BASE / 'song_overview.csv'
    labels_path = BASE / 'current-public-download.csv'
    originals = list(csv.DictReader(original_path.open(newline='')))
    current = list(csv.DictReader(labels_path.open(encoding='cp1252', newline='')))
    lookup = {}
    for row in originals:
        for artist in artist_keys(row['artist']):
            lookup.setdefault((artist, identity(row['title'])), {})[row['code']] = row
    matches, rejected = [], []
    for row in current:
        if row.get('GEMS Version') != 'gems-45_mx':
            rejected.append({'row': row.get(''), 'reason': 'not_original_GEMS45_or_malformed_export_row'})
            continue
        hits = {}
        for artist in artist_keys(row['artist']):
            hits.update(lookup.get((artist, identity(row['title'])), {}))
        if len(hits) != 1:
            rejected.append({'row': row[''], 'artist': row['artist'], 'title': row['title'],
                             'reason': 'ambiguous_or_unmatched_original_identity', 'matches': sorted(hits)})
            continue
        code, original = next(iter(hits.items()))
        if not re.fullmatch(r'[A-Za-z0-9_\-]+', code):
            raise ValueError('Unexpected public audio code: ' + code)
        targets = [float(row[name]) / 100 for name in TARGETS]
        if any(not 0 <= value <= 1 for value in targets):
            raise ValueError('EMMA emotion intensity outside the published 0–100 scale')
        matches.append({'code': code, 'artist': row['artist'], 'title': row['title'],
                        'targets': targets, 'rater_count': int(row['Number of Raters']),
                        'icc': float(row['ICC']), 'original': original, 'csv_row': row['']})
    if len({row['code'] for row in matches}) != len(matches):
        raise ValueError('Several current annotations match one original audio code')
    audit = {'created_at': stamp(), 'current_export_rows': len(current), 'original_metadata_rows': len(originals),
             'matched_rows': len(matches), 'rejected': rejected, 'annotation_sha256': digest(labels_path),
             'metadata_sha256': digest(original_path), 'targets': list(TARGETS),
             'selection': 'GEMS-45 rows only; strict Unicode-normalized artist/title identity join with one original audio code. No fuzzy matching, developer labels, or predictions.',
             'audio_url_template': 'https://musemap-tools.uibk.ac.at/emma/songs/{code}.mp3',
             'annotation_url': 'https://musemap-tools.uibk.ac.at/emma/ (public Expert CSV download)',
             'paper': 'https://doi.org/10.3758/s13428-024-02336-0',
             'limitations': ['Current public previews are about 15 seconds, shorter than the original 30–60-second rated excerpts. Precise preview alignment is unverified; treat annotations as weak supervision.',
                            'The frozen current public export is not asserted identical to the original OSF analysis table.',
                            'No access to unshared original sound files; native performance is a preview-transfer experiment, not the original EMMA benchmark.',
                            'Liking and familiarity are excluded from features and targets. Public availability does not establish commercial redistribution rights.']}
    save(BASE / 'join-audit.json', audit)
    return matches, audit


def download_plan():
    rows, audit = joined()
    audio = BASE / 'audio'
    audio.mkdir(exist_ok=True)
    config = []
    for row in rows:
        target = audio / (row['code'] + '.mp3')
        if target.exists() and target.stat().st_size > 10000:
            continue
        if config:
            config.append('next')
        config += ['fail', 'silent', 'show-error', 'location', 'connect-timeout = 10', 'max-time = 35',
                   'retry = 1', 'url = "' + audit['audio_url_template'].format(code=quote(row['code'])) + '"',
                   'output = "/data/audio/' + row['code'] + '.mp3"']
    (BASE / 'preview-download.conf').write_text('\n'.join(config) + '\n')
    print(json.dumps({'matches': len(rows), 'downloads_queued': sum(line.startswith('url =') for line in config)}))


def prepare_rows():
    from .external_experiment import probe
    joined_rows, audit = joined()
    rows, unavailable = [], []
    for item in joined_rows:
        audio = BASE / 'audio' / (item['code'] + '.mp3')
        if not audio.exists():
            unavailable.append({'code': item['code'], 'reason': 'unavailable_preview'})
            continue
        try:
            duration, _ = probe(audio)
        except Exception as error:
            unavailable.append({'code': item['code'], 'reason': repr(error)})
            continue
        if not 10 <= duration <= 65:
            unavailable.append({'code': item['code'], 'reason': 'unexpected_preview_duration', 'seconds': duration})
            continue
        rows.append({'id': 'emma-preview:' + item['code'], 'audio': str(audio),
                     'group': 'emma:' + identity(item['artist']), 'artist': item['artist'], 'title': item['title'],
                     'target_names': [name.lower() for name in TARGETS], 'targets': item['targets'],
                     'native_scale': [0, 100], 'annotation_unit': 'longer_rated_excerpt_weak_label_for_current_preview',
                     'rater_count': item['rater_count'], 'annotation_icc': item['icc'],
                     'annotation_duration': (float(item['original']['duration_sec'])
                                             if item['original']['duration_sec'] not in ('', 'NA') else None),
                     'original_excerpt_start': item['original']['timestamp_start'],
                     'source_genre': item['original']['genre'], 'preview_seconds': duration})
    if len(rows) < 30:
        raise ValueError('Too few available, decodable EMMA previews')
    save(OUT / 'emma-preview-matched.join-audit.json', {**audit, 'available_rows': len(rows),
         'unavailable': unavailable, 'genres': dict(Counter(row['source_genre'] for row in rows)),
         'preview_duration_range': [min(row['preview_seconds'] for row in rows), max(row['preview_seconds'] for row in rows)],
         'audio_sha256': {row['id']: digest(row['audio']) for row in rows}})
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'prepare'])
    args = parser.parse_args()
    download_plan() if args.action == 'plan' else print(json.dumps({'rows': len(prepare_rows())}))
