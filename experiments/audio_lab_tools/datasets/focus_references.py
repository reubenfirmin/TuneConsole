"""Bounded Focus annotation acquisition; no audio recovery or model selection."""
import argparse
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

from .dataset_leads import WORK, fetch, questionnaire_sheets
from ..support.external_experiment import OUT, digest, save, stamp
from .external_secondary import identity

DEFAULT = WORK / 'focus-576'
FILES = {
    'moods-activities/Data.zip': ('https://reshare.ukdataservice.ac.uk/852024/1/Data.zip', None),
    'moods-activities/ReadMe.txt': ('https://reshare.ukdataservice.ac.uk/852024/3/ReadMe.txt', None),
    'study/esm_raw_data.xlsx': ('https://zenodo.org/records/10085104/files/esm_raw_data.xlsx?download=1', 'c8116faa7c0451fe10c9042648c6febf'),
    'study/esm_music_features_analysis.xlsx': ('https://zenodo.org/records/10085104/files/esm_music_features_analysis.xlsx?download=1', 'af088b65cf32c7a9678446d2483c0071'),
    'study/index.xlsx': ('https://zenodo.org/records/10085104/files/index.xlsx?download=1', '79ed928bea95bf75a958043789a658e5'),
}


def acquire():
    result = {'created_at': stamp(), 'files': {}, 'audio_recovery': False}
    for name, (url, checksum) in FILES.items():
        path = DEFAULT / name
        try:
            content = fetch(url, path, limit=8 * 1024 * 1024, md5=checksum)
            result['files'][name] = {'bytes': len(content), 'sha256': digest(path), 'url': url}
        except (OSError, ValueError) as exc:
            result['files'][name] = {'url': url, 'error': str(exc)}
        print(json.dumps({name: result['files'][name]}), flush=True)
    save(DEFAULT / 'acquisition-report.json', result)


def inspect():
    result = {}
    for name in FILES:
        path = DEFAULT / name
        if not path.exists():
            result[name] = {'missing': True}
        elif path.suffix == '.txt':
            result[name] = {'sha256': digest(path), 'text': path.read_text(errors='replace')}
        else:
            with zipfile.ZipFile(path) as archive:
                members = [{'name': p.filename, 'bytes': p.file_size} for p in archive.infolist()]
                if path.suffix == '.xlsx':
                    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                    tree = ET.fromstring(archive.read('xl/workbook.xml'))
                    result[name] = {'sha256': digest(path), 'sheets': [p.attrib for p in tree.findall('s:sheets/s:sheet', ns)]}
                else:
                    result[name] = {'sha256': digest(path), 'members': members}
    save(DEFAULT / 'inspection.json', result)
    print(json.dumps(result, indent=2))


def audit():
    """Inventory native tasks and exact text joins, without promoting versions."""
    result = {'created_at': stamp(), 'sources': {}, 'audio_recovery': False}
    for name in FILES:
        path = DEFAULT / name
        if path.exists() and digest(path) != json.loads(path.with_suffix(path.suffix + '.acquisition.json').read_text())['sha256']:
            raise ValueError('Acquired annotation changed: ' + name)
    lookup = defaultdict(list)
    inputs = {}
    for dataset in ('emotify-artist', 'cal500-raw-activity', 'pmemo', 'deam', 'soundtracks-all',
                    'jamendo-human-4shards', 'coimbra-mirex-like', '4q-emotion', 'merge-quadrants',
                    'vgmidi-original', 'emma-preview-matched', 'emotify-plus', 'merp'):
        path = OUT / (dataset + '.features.json')
        if not path.exists():
            continue
        inputs[str(path)] = digest(path)
        for row in json.loads(path.read_text())['rows']:
            key = identity(row.get('artist') or ''), identity(row.get('title') or '')
            if all(key):
                lookup[key].append({k: row.get(k) for k in ('id', 'artist', 'title', 'audio_sha256',
                    'sample_start', 'sample_seconds', 'annotation_unit')} | {'dataset': dataset})
    path = DEFAULT / 'moods-activities/Data.zip'
    if path.exists():
        with zipfile.ZipFile(path) as archive:
            def table(name):
                item = archive.getinfo('moods_and_activities_in_music/' + name + '.csv')
                if item.file_size > 8 * 1024 * 1024:
                    raise ValueError('CSV exceeds annotation bound')
                return list(csv.DictReader(io.StringIO(archive.read(item).decode('utf-8-sig'))))
            activities, tracks, tags, searches = [table(name) for name in ('activities', 'tracks', 'tags', 'searches')]
        columns = [key for key in tags[0] if key.startswith('activity.')]
        by_track = defaultdict(list)
        for row in tags:
            by_track[row['trackid']].append(row)
        matched = []
        for track in tracks:
            key = identity(track['artistname']), identity(track['tracktitle'])
            if key not in lookup:
                continue
            ratings = by_track[track['trackid']]
            # Collapse repeated answers by the same listener before averaging.
            listeners = defaultdict(list)
            for row in ratings:
                listeners[row['userid']].append(row)
            fractions = {}
            for column in columns:
                means = []
                for values in listeners.values():
                    valid = [float(row[column]) for row in values if row[column] in ('0', '1')]
                    if valid:
                        means.append(sum(valid) / len(valid))
                fractions[column] = {'mean': sum(means) / len(means) if means else None, 'listeners': len(means)}
            matched.append({'track': track, 'listened_tag_rows': len(ratings),
                            'listener_fractions': fractions, 'audio_candidates': lookup[key],
                            'verified_recording_and_excerpt': False})
        result['sources']['moods_activities'] = {'sha256': digest(path), 'activities': activities,
            'tracks': len(tracks), 'listened_tag_rows': len(tags), 'nomination_rows': len(searches),
            'annotation_columns': columns, 'text_matched_tracks': len(matched),
            'matched_with_listened_tags': sum(bool(row['listened_tag_rows']) for row in matched),
            'verified_audio_window_matches': 0, 'matches': matched,
            'limits': ['Artist/title equality is a candidate identity, not verified 7digital recording/version equality.',
                       'No supplied audio or tagged excerpt positions in these files; existing excerpt equality is unestablished.',
                       'Mood-word activity associations and nomination tasks are excluded from listened-track references.',
                       'Broad intellectual activity must not silently replace the Reading/Studying definition.']}
    for name in ('esm_raw_data', 'esm_music_features_analysis', 'index'):
        path = DEFAULT / f'study/{name}.xlsx'
        if not path.exists():
            continue
        sheets = questionnaire_sheets(path)
        result['sources'][name] = {'sha256': digest(path), 'sheets': {key: {
            'rows': len(rows), 'headers': rows[0] if rows else [],
            'definitions': rows[1:] if name == 'index' else []} for key, rows in sheets.items()}}
        if name == 'esm_raw_data':
            data = next(iter(sheets.values()))
            records = [dict(zip(data[0], row + [''] * len(data[0]))) for row in data[1:]]
            joins, seen = [], set()
            for record in records:
                for prefix in ('InitStart', 'InitEnd', '2min', '4min', 'MainStart', 'MainEnd', '8min'):
                    artist, title = record.get(prefix + 'Artist', ''), record.get(prefix + 'Song', '')
                    key = identity(artist), identity(title)
                    if not all(key):
                        continue
                    seen.add(key)
                    if key in lookup:
                        joins.append({'artist': artist, 'title': title, 'spotify_id': record.get(prefix + 'SpotifyID'),
                                      'prompt': prefix, 'audio_candidates': lookup[key],
                                      'verified_recording_and_excerpt': False})
            result['sources'][name].update(unique_artist_title_pairs=len(seen),
                matched_episode_track_fields=len(joins),
                unique_text_matches=len({(identity(r['artist']), identity(r['title'])) for r in joins}),
                matches=joins, verified_audio_window_matches=0,
                limits=['Episode-level effects can cover multiple songs; no automatic single-track attribution.',
                        'Spotify/audio feature estimates are not human suitability references.',
                        'Existing-audio text matches do not establish recording version or observed playback-window identity.'])
    result['input_features'] = inputs
    save(DEFAULT / 'reference-audit.json', result)
    compact = {'created_at': result['created_at'], 'input_features': len(inputs), 'sources': {key: {
        k: v for k, v in value.items() if k not in ('matches', 'sheets', 'activities')}
        for key, value in result['sources'].items()}}
    print(json.dumps(compact, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('acquire', 'inspect', 'audit'))
    args = parser.parse_args()
    {'acquire': acquire, 'inspect': inspect, 'audit': audit}[args.action]()


if __name__ == '__main__':
    main()
