"""Bounded publisher metadata and existing-audio intersections for dataset leads.

This command does not download audio, train a model, or infer labels from text.
Cached responses retain their exact URLs, response hashes and publisher checksums.
"""
import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import posixpath
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from .dataset_range import RemoteZip
from .external_experiment import DATA, OUT, digest, save, stamp

WORK = DATA / 'discovery'
SDD_FILES = {
    'song_describer.csv': 'e90e9459c22bfbe69f5462dc1434d573',
    'audio_metadata.tsv': 'fa5d126659b08680719d189387d61e39',
    'audio_licenses.txt': 'cda13a080c03e14b8319430e1486ae6e',
    'song_describer_14_04_23.mtg-jamendo.tsv': '3532f2df8b4c21a7ea85d9121eae244a',
}


def fetch(url, path, *, limit, md5=None):
    record_path = path.with_suffix(path.suffix + '.acquisition.json')
    if path.exists() and record_path.exists():
        record = json.loads(record_path.read_text())
        if record['url'] != url or digest(path) != record['sha256']:
            raise ValueError('Publisher metadata cache changed: ' + str(path))
        content = path.read_bytes()
    else:
        request = urllib.request.Request(url, headers={
            'User-Agent': 'TuneConsole-dataset-research/1', 'Accept-Encoding': 'identity'})
        with urllib.request.urlopen(request, timeout=45) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ValueError('Publisher response exceeds metadata budget')
            record = {'created_at': stamp(), 'url': url, 'final_url': response.url,
                      'bytes': len(content), 'etag': response.headers.get('ETag'),
                      'sha256': hashlib.sha256(content).hexdigest()}
        if md5 and hashlib.md5(content).hexdigest() != md5:
            raise ValueError('Publisher metadata MD5 differs: ' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        save(record_path, {**record, 'publisher_md5': md5})
    if len(content) > limit or (md5 and hashlib.md5(content).hexdigest() != md5):
        raise ValueError('Cached publisher metadata fails its acquisition bound/checksum')
    return content


def acquire(dataset):
    target = WORK / dataset
    if dataset == 'song-describer':
        for filename, checksum in SDD_FILES.items():
            fetch('https://zenodo.org/records/10072001/files/' + filename + '?download=1',
                  target / filename, limit=1024 * 1024, md5=checksum)
    elif dataset == 'globalmood':
        commit = json.loads(fetch('https://api.github.com/repos/harin-git/GlobalMood/commits/main',
                                  target / 'publisher-commit.json', limit=2 * 1024 * 1024))['sha']
        tree = json.loads(fetch(f'https://api.github.com/repos/harin-git/GlobalMood/git/trees/{commit}?recursive=1',
                                target / 'publisher-tree.json', limit=2 * 1024 * 1024))
        if tree.get('truncated'):
            raise ValueError('Publisher file tree is incomplete')
        entries = {row['path']: row for row in tree['tree']}
        for filename in ('README.md', 'LICENSE', 'Data/songmeta_GlobalMood.csv', 'Data/meanrating_GlobalMood.csv'):
            content = fetch(f'https://raw.githubusercontent.com/harin-git/GlobalMood/{commit}/{filename}',
                            target / filename, limit=12 * 1024 * 1024)
            git_blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
            if git_blob != entries[filename]['sha']:
                raise ValueError('Publisher Git blob differs: ' + filename)
    elif dataset == 'playing-musicians':
        source = target / 'publisher-info.json'
        info = json.loads(fetch('https://zenodo.org/api/records/10396364', source,
                               limit=4 * 1024 * 1024))
        item = next(row for row in info['files'] if row['key'] == 'dataset.zip')
        reader = RemoteZip(item['links']['self'], item['size'], target, budget=16 * 1024 * 1024)
        with zipfile.ZipFile(reader) as archive:
            members, extracted = [], []
            for item in archive.infolist():
                name = Path(item.filename)
                if name.is_absolute() or '..' in name.parts:
                    raise ValueError('Unsafe musician archive member')
                members.append({'name': item.filename, 'bytes': item.file_size,
                    'compressed_bytes': item.compress_size, 'crc32': f'{item.CRC:08x}'})
                # Questionnaires and documentation only. Acoustic features and
                # physiological files are unnecessary for deciding the task.
                if ('__MACOSX' in name.parts or item.file_size > 2 * 1024 * 1024 or
                        name.suffix.lower() not in ('.xls', '.xlsx', '.pdf', '.txt', '.md')):
                    continue
                path = target / 'metadata' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(item.filename))
                extracted.append({'name': item.filename, 'sha256': digest(path),
                    'bytes': item.file_size, 'zip_crc32_verified': True})
        save(target / 'archive-inspection.json', {'created_at': stamp(),
            'publisher_metadata_sha256': digest(source), 'archive': {
                'url': reader.url, 'bytes': reader.size,
                'published_checksum_not_whole_file_verified': next(
                    row['checksum'] for row in info['files'] if row['key'] == 'dataset.zip')},
            'members': members, 'extracted_metadata': extracted,
            'new_network_bytes': reader.received, 'ranges': reader.ranges,
            'audio_acquired': False, 'labels_parsed': False,
            'licence': info.get('metadata', {}).get('license'),
            'limitations': ['Archive directory and small documentation only; no audio decoding or label joining.',
                'Split performances are dependent observations; intended, communicated and performer-felt emotion differ.',
                'This release does not establish listener anger regulation or six-vibe suitability.']})
    else:
        info = json.loads(fetch('https://huggingface.co/api/datasets/ecnu-aigc/EMID',
                               target / 'publisher-info.json', limit=4 * 1024 * 1024))
        revision = info['sha']
        if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('Expected a full publisher revision')
        for filename in ('README.md', 'EMID_data.csv'):
            fetch(f'https://huggingface.co/datasets/ecnu-aigc/EMID/resolve/{revision}/{filename}',
                  target / filename, limit=16 * 1024 * 1024)
    inspect(dataset)


def globalmood_inspection(target):
    from .external_secondary import identity
    path = target / 'Data/songmeta_GlobalMood.csv'
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
    lookup = {}
    for row in rows:
        key = (identity(row['artist']), identity(row['song']))
        if all(key):
            lookup.setdefault(key, []).append(row)
    matches, inputs = {}, {}
    for name in ('soundtracks-all', 'cal500', 'emotify-artist', 'pmemo', 'vgmidi-original',
                 'jamendo-human-4shards', 'coimbra-mirex-like', '4q-emotion', 'deam', 'merge-quadrants'):
        feature_path = OUT / (name + '.features.json')
        if not feature_path.exists():
            continue
        inputs[name] = digest(feature_path)
        for audio in json.loads(feature_path.read_text())['rows']:
            key = (identity(audio.get('artist') or ''), identity(audio.get('title') or ''))
            for row in lookup.get(key, []):
                matches.setdefault(row['videoID'], {'metadata': row, 'audio_candidates': []})['audio_candidates'].append({
                    'dataset': name, 'id': audio['id'], 'audio_sha256': audio.get('audio_sha256'),
                    'group': audio.get('group')})
    ratings_path = target / 'Data/meanrating_GlobalMood.csv'
    ratings = list(csv.DictReader(io.StringIO(ratings_path.read_text(encoding='utf-8-sig'))))
    missing, values = Counter(), {name: [] for name in ('mean_rating', 'sd_rating', 'n_ratings')}
    for row in ratings:
        for name, column in values.items():
            try:
                value = float(row[name])
            except ValueError:
                missing[name] += 1
                continue
            if not math.isfinite(value):
                missing[name] += 1
            else:
                column.append(value)
    rating_countries = sorted({row['country'] for row in ratings})
    report = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'publisher_revision': json.loads((target / 'publisher-commit.json').read_text())['sha'],
        'metadata_sha256': digest(path), 'ratings_sha256': digest(ratings_path),
        'metadata_rows': len(rows), 'unique_video_ids': len({row['videoID'] for row in rows}),
        'song_country_counts': dict(Counter(row['country'] for row in rows)),
        'rating_rows': len(ratings), 'rating_columns': list(ratings[0]) if ratings else [],
        'rating_example': ratings[0] if ratings else None,
        'rating_country_counts': dict(Counter(row['country'] for row in ratings)),
        'rating_tags_per_country': {country: len({row['tag'] for row in ratings if row['country'] == country})
                                    for country in rating_countries},
        'rating_video_ids_absent_from_metadata': sorted({row['videoID'] for row in ratings} - {row['videoID'] for row in rows}),
        'rating_missing_or_nonfinite_fields': dict(missing),
        'finite_rating_field_ranges': {name: [min(column), max(column)] for name, column in values.items() if column},
        'source_features_sha256': inputs, 'exact_artist_title_matches': len(matches),
        'matches': list(matches.values()), 'audio_acquired': False, 'targets_derived_from_text': False,
        'policy': 'Bounded exact Unicode-normalized complete artist/title intersection with already acquired feature manifests. No fuzzy matching, artist splitting, audio recovery, fitting or six-vibe translation.',
        'limitations': ['A matched title and complete artist credit do not verify the same recording, master or heard excerpt.',
            'Country of song curation and country/language of rating participants are distinct.',
            'Known earlier-corpus matches cannot serve as independent new-audio holdouts.',
            'Released code/annotation licence does not establish commercial rights to source recordings.']}
    save(target / 'metadata-inspection.json', report)
    print(json.dumps({key: value for key, value in report.items() if key not in ('matches', 'source_features_sha256')}), flush=True)
    return report


def questionnaire_sheets(path):
    """Read this small publisher XLSX without adding a spreadsheet dependency."""
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with zipfile.ZipFile(path) as book:
        if sum(row.file_size for row in book.infolist()) > 8 * 1024 * 1024:
            raise ValueError('Questionnaire workbook exceeds metadata bound')
        strings = ([] if 'xl/sharedStrings.xml' not in book.namelist() else
            [''.join(node.itertext()) for node in ET.fromstring(book.read('xl/sharedStrings.xml'))])
        relationships = {row.attrib['Id']: row.attrib['Target']
                         for row in ET.fromstring(book.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(book.read('xl/workbook.xml')).find(ns + 'sheets'):
            relationship = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            filename = relationships[relationship]
            filename = (filename.lstrip('/') if filename.startswith('/') else
                        posixpath.normpath('xl/' + filename))
            rows = []
            for row in ET.fromstring(book.read(filename)).iter(ns + 'row'):
                values = []
                for cell in row:
                    letters = ''.join(char for char in cell.attrib['r'] if char.isalpha())
                    column = 0
                    for char in letters:
                        column = column * 26 + ord(char.upper()) - ord('A') + 1
                    while len(values) < column:
                        values.append('')
                    raw = cell.findtext(ns + 'v', default='')
                    values[column - 1] = (strings[int(raw)] if cell.get('t') == 's' else
                        ''.join(cell.itertext()) if cell.get('t') == 'inlineStr' else raw)
                rows.append(values)
            result[sheet.attrib['name']] = rows
        return result


def inspect(dataset):
    target = WORK / dataset
    if dataset == 'playing-musicians':
        report = json.loads((target / 'archive-inspection.json').read_text())
        if digest(target / 'publisher-info.json') != report['publisher_metadata_sha256']:
            raise ValueError('Musician publisher metadata changed')
        for row in report['extracted_metadata']:
            path = target / 'metadata' / row['name']
            if digest(path) != row['sha256'] or path.stat().st_size != row['bytes']:
                raise ValueError('Musician questionnaire/documentation changed')
        wavs = [row for row in report['members'] if row['name'].lower().endswith('.wav')]
        sheet_path = next(target / 'metadata' / row['name'] for row in report['extracted_metadata']
                          if row['name'].endswith('.xlsx'))
        sheets = questionnaire_sheets(sheet_path)
        headers = ['Subject', 'recording_filename', 'intended_emotion', 'valence_communicated',
                   'arousal communicated', 'valence_felt', 'arousal_felt', 'instrument_played']
        if sheets['Sheet1'][0] != headers:
            raise ValueError('Musician questionnaire headers changed')
        rows = [dict(zip(headers, row + [''] * len(headers)))
                for row in sheets['Sheet1'][1:] if any(value.strip() for value in row)]
        originals = {Path(row['name']).stem: row for row in wavs
                     if Path(row['name']).parent.name == 'wav_audio_files_original'}
        recording_ids = [row['recording_filename'] for row in rows]
        joined = [row for row in rows if row['recording_filename'] in originals]
        invalid = []
        for row in rows:
            for column in headers[3:7]:
                try:
                    value = float(row[column])
                except ValueError:
                    value = math.nan
                if not math.isfinite(value) or not 1 <= value <= 5 or not value.is_integer():
                    invalid.append({'recording': row['recording_filename'], 'field': column,
                                    'value': row[column]})
        summary = {'created_at': stamp(), 'script_sha256': digest(__file__),
            'publisher_metadata_sha256': report['publisher_metadata_sha256'],
            'archive_inspection_sha256': digest(target / 'archive-inspection.json'),
            'questionnaire_sha256': digest(sheet_path),
            'dataset': dataset, 'archive_members': len(report['members']),
            'wav_members': len(wavs), 'wav_parent_counts': dict(Counter(str(Path(row['name']).parent) for row in wavs)),
            'extracted_metadata': report['extracted_metadata'], 'licence': report['licence'],
            'new_network_bytes_at_acquisition': report['new_network_bytes'],
            'questionnaire_sheets': {name: {'xml_rows_including_formatting': len(data),
                'nonempty_rows': sum(any(value.strip() for value in row) for row in data)}
                for name, data in sheets.items()},
            'native_label_rows': len(rows), 'unique_recordings': len(set(recording_ids)),
            'labelled_original_members': len(joined),
            'joined_subject_counts': dict(Counter(row['Subject'] for row in joined)),
            'joined_intended_emotion_counts': dict(Counter(row['intended_emotion'] for row in joined)),
            'duplicate_recording_ids': [key for key, count in Counter(recording_ids).items() if count > 1],
            'subject_counts': dict(Counter(row['Subject'] for row in rows)),
            'intended_emotion_counts': dict(Counter(row['intended_emotion'] for row in rows)),
            'instrument_counts': dict(Counter(row['instrument_played'] for row in rows)),
            'labels_without_original_member': sorted(set(recording_ids) - originals.keys()),
            'original_members_without_label': sorted(originals.keys() - set(recording_ids)),
            'invalid_native_ratings': invalid, 'native_labels': rows,
            'audio_acquired': False, 'audio_decoded': False, 'six_vibe_labels_created': False,
            'limitations': report['limitations'][1:] + [
                'Only Sheet1 supplies the eight documented annotation fields; other sheets and formatting rows are not observations.',
                'Filename joins identify supplied members but do not verify audio duration or acoustic content.',
                'The published archive checksum is unverified; extracted questionnaire members passed ZIP CRC.']}
        save(target / 'metadata-inspection.json', summary)
        print(json.dumps({key: value for key, value in summary.items() if key != 'native_labels'}), flush=True)
        return summary
    if dataset == 'song-describer':
        expected = {target / filename: checksum for filename, checksum in SDD_FILES.items()}
    elif dataset == 'globalmood':
        expected = {target / filename: None for filename in ('publisher-commit.json', 'publisher-tree.json',
                    'README.md', 'LICENSE', 'Data/songmeta_GlobalMood.csv', 'Data/meanrating_GlobalMood.csv')}
    else:
        expected = {target / filename: None for filename in ('publisher-info.json', 'README.md', 'EMID_data.csv')}
    for cached_path, md5 in expected.items():
        record = json.loads(cached_path.with_suffix(cached_path.suffix + '.acquisition.json').read_text())
        if digest(cached_path) != record['sha256'] or (md5 and hashlib.md5(cached_path.read_bytes()).hexdigest() != md5):
            raise ValueError('Acquired publisher metadata changed: ' + str(cached_path))
    if dataset == 'globalmood':
        return globalmood_inspection(target)
    path = target / ('song_describer.csv' if dataset == 'song-describer' else 'EMID_data.csv')
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
    report = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'metadata_sha256': digest(path), 'rows': len(rows),
        'columns': list(rows[0]) if rows else [],
        'audio_acquired': False, 'targets_derived_from_text': False}
    if dataset == 'emid':
        info = json.loads((target / 'publisher-info.json').read_text())
        report['publisher_revision'] = info['sha']
        report['published_files'] = [row['rfilename'] for row in info['siblings']]
        originals = [row for row in rows if row['is_original_clip'] == 'True']
        augmented = [row for row in rows if row['is_original_clip'] == 'False']
        if len(originals) + len(augmented) != len(rows):
            raise ValueError('Unexpected original/augmented flag')
        annotations = {(row['feeling'], row['emotion']) for row in originals}
        original_by_name = {row['Audio_Filename']: row for row in originals}
        parents, unresolved, different = Counter(), [], []
        for row in augmented:
            name = row['Audio_Filename'].rsplit('___', 1)[0] + '.mp3'
            parent = original_by_name.get(name)
            if parent is None:
                unresolved.append(row['Audio_Filename'])
                continue
            parents[name] += 1
            if (parent['feeling'], parent['emotion']) != (row['feeling'], row['emotion']):
                different.append(row['Audio_Filename'])
        invalid = []
        for row in rows:
            values = [float(value) for value in row['emotion'].split(',')]
            if len(values) != 11 or any(not math.isfinite(value) or not 1 <= value <= 9 for value in values):
                invalid.append({'audio': row['Audio_Filename'], 'raw_values': row['emotion'],
                                'original': row['is_original_clip'] == 'True'})
        report.update({
            'unique_audio_filenames': len({row['Audio_Filename'] for row in rows}),
            'original_clips': len(originals), 'augmented_clips': len(augmented),
            'original_category_counts': dict(Counter(row['genre'] for row in originals)),
            'original_annotation_bundles': len(annotations),
            'augmented_rows_sharing_exact_original_annotation_bundle': sum(
                (row['feeling'], row['emotion']) in annotations for row in augmented),
            'augmented_rows_joined_to_original_filename': sum(parents.values()),
            'unresolved_augmented_parent_filenames': unresolved,
            'joined_augmented_annotation_disagreements': different,
            'originals_with_augmented_children': len(parents),
            'invalid_eleven_dimension_rating_rows': invalid,
            'example_augmented_filenames': [row['Audio_Filename'] for row in augmented[:8]],
            'limitations': ['Original and augmented rows are not independent human listening judgments.',
                'Filename joins establish publisher grouping, not identical audio content or fresh listener judgments.',
                'Original audio provenance, excerpt boundaries and named dimension ordering need verification.',
                'Image tags and generated image descriptions are not human judgments of the music.']})
    else:
        def numeric(value):
            return str(int(value.rsplit('_', 1)[-1]))
        references = {}
        for name, source in (
                ('jamendo_human', OUT / 'jamendo-human-4shards.index.json'),
                ('manymusic', WORK / 'manymusic/human-window-index.json')):
            index = json.loads(source.read_text())
            candidates = index['rows']
            tracks = {numeric(row['id']) for row in candidates} if name == 'jamendo_human' else {
                numeric(row['recording_id'].split(':', 1)[1]) for row in candidates if not row['synthetic_audio']}
            artists = {numeric(row['source_artist_id']) for row in candidates} if name == 'jamendo_human' else {
                numeric(row['artist_id']) for row in candidates if not row['synthetic_audio']}
            references[name] = (tracks, artists)
            report.setdefault('overlap_input_sha256', {})[str(source)] = digest(source)
        report['subsets'] = {}
        for name, retained in (('full', rows), ('validated', [row for row in rows if row['is_valid_subset'] == 'True'])):
            by_track = {numeric(row['track_id']): row for row in retained}
            all_tracks = set().union(*(tracks for tracks, _artists in references.values()))
            all_artists = set().union(*(artists for _tracks, artists in references.values()))
            report['subsets'][name] = {
                'captions': len(retained), 'tracks': len(by_track),
                'artists': len({numeric(row['artist_id']) for row in retained}),
                'tracks_without_known_track_or_artist_overlap': sum(
                    key not in all_tracks and numeric(row['artist_id']) not in all_artists
                    for key, row in by_track.items()),
                'known_overlap': {source: {
                    'same_track_ids': sorted(set(by_track) & tracks),
                    'tracks_with_known_track_or_artist_overlap': sum(
                        key in tracks or numeric(row['artist_id']) in artists for key, row in by_track.items())}
                    for source, (tracks, artists) in references.items()}}
        report['limitations'] = ['Human free-text captions do not supply calibrated six-vibe targets.',
            'An absent descriptor is unknown rather than an explicit negative.',
            'Publisher intends this single split for evaluation; captions should remain an external holdout.',
            'Jamendo identity overlap is checked, but aliases and backbone pretraining remain unresolved.']
    save(target / 'metadata-inspection.json', report)
    shown = {key: ({'count': len(value), 'first_five': value[:5]}
                   if isinstance(value, list) and len(value) > 20 else value)
             for key, value in report.items()}
    print(json.dumps({'dataset': dataset, **shown}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['acquire', 'inspect'])
    parser.add_argument('dataset', choices=['song-describer', 'emid', 'globalmood', 'playing-musicians'])
    args = parser.parse_args()
    (acquire if args.action == 'acquire' else inspect)(args.dataset)


if __name__ == '__main__':
    main()
