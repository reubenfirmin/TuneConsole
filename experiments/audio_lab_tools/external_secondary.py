"""Recover additional native annotations for supplied audio with exact identities."""
from bisect import bisect_right
import csv
import hashlib
import io
import json
from pathlib import Path
import unicodedata
import zipfile

import numpy as np
from scipy.io import loadmat

from .external_experiment import DATA, OUT, digest, save, stamp

AMG_COMMIT = '16ce26147f509e712c70cef99dc7dd0dfa912702'
AMG_PART_SHA1 = [
    'b047bc0c4c57c98f1327db479887683c4daaf197', 'c83a3455634029132de6651a994a1cf21de1c7bd',
    'f40c13f40b541e970efc8a194c1625ef7d6f65a4', 'eb4e0cbe086a84f5c718463b2201c401a0ee141d',
    'ab995c5f8188fe6e9218846b652be16faf019da0', 'e473a3d433859eaafda9c3ed50ef6874d59949e5',
    'a49c6492c3a132759f1e06da88bbe00e94f396d0', 'efe105c780519ff3684acdbf42e192de102e3a03',
    '4fef54426ded56051b2f8062324c18f8c55c16ca', '8369e0f8d8c72760d73a01dfc2e9b0e4a8da6fd0',
    '4ede455fb793be5691c7b97d57c28be51a696d2f', '290704d7cb4f1fc9de03f2e0144cf182da22a615']


def identity(text):
    return ''.join(char for char in unicodedata.normalize('NFKC', text).casefold() if char.isalnum())


def artist_keys(text):
    result = {identity(text)}
    if text.count(',') == 1:
        surname, given = text.split(',')
        result.add(identity(given + ' ' + surname))
    return result - {''}


class MultipartReader(io.RawIOBase):
    """Read raw split ZIP volumes without writing a second gigabyte archive."""
    def __init__(self, paths):
        self.streams = [Path(path).open('rb') for path in paths]
        self.offsets = [0]
        for path in paths:
            self.offsets.append(self.offsets[-1] + Path(path).stat().st_size)
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.offsets[-1] + offset
        if position < 0:
            raise ValueError('Negative multipart seek')
        self.position = position
        return position

    def read(self, size=-1):
        size = max(0, self.offsets[-1] - self.position) if size < 0 else min(size, max(0, self.offsets[-1] - self.position))
        chunks = []
        while size:
            index = bisect_right(self.offsets, self.position) - 1
            self.streams[index].seek(self.position - self.offsets[index])
            chunk = self.streams[index].read(min(size, self.offsets[index + 1] - self.position))
            if not chunk:
                raise EOFError('Truncated split ZIP volume')
            chunks.append(chunk)
            self.position += len(chunk)
            size -= len(chunk)
        return b''.join(chunks)

    def close(self):
        for stream in self.streams:
            stream.close()
        super().close()


def stage_amg(remove_parts=False):
    base = DATA / 'amg1608/mirror'
    destination = base / 'annotations.npz'
    if destination.exists():
        acquisition = json.loads((base / 'annotation-acquisition.json').read_text())
        if digest(destination) != acquisition['annotations_sha256']:
            raise ValueError('Stored AMG annotation checksum changed')
        return
    paths = [base / f'AMG1608Mat.zip.{index:03d}' for index in range(1, 13)]
    artifacts = []
    for path, expected in zip(paths, AMG_PART_SHA1):
        size = path.stat().st_size
        check = hashlib.sha1(f'blob {size}\0'.encode())
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                check.update(block)
        if check.hexdigest() != expected:
            raise ValueError('AMG split volume does not match pinned Git blob: ' + path.name)
        artifacts.append({'file': path.name, 'bytes': size, 'git_blob_sha1': expected, 'sha256': digest(path)})
    with MultipartReader(paths) as combined, zipfile.ZipFile(combined) as archive:
        files = archive.namelist()
        members = [name for name in files if name.lower().endswith('.mat')]
        if len(members) != 1:
            raise ValueError('Expected one original AMG MAT file')
        with archive.open(members[0]) as source:
            values = loadmat(source, variable_names=['song_info', 'song_label'], simplify_cells=True)
        # Consume the remaining bytes to establish the member CRC without ever
        # materializing the unused frame-level acoustic feature matrix.
            while source.read(1024 * 1024):
                pass
    labels = np.asarray(values['song_label'], dtype=float)
    info = np.asarray(values['song_info'])
    if labels.shape != (1608, 665, 2) or info.shape != (1608, 2):
        raise ValueError('AMG release shapes disagree with its README')
    finite = np.isfinite(labels)
    if finite.sum() == 0 or (np.abs(labels[finite]) > 1.05).any():
        raise ValueError('AMG ratings exceed observed native coordinate range')
    # Missing ratings must be explicit, rather than guessed from midpoint zero.
    if finite.mean() > .5:
        raise ValueError('AMG missing-value representation needs inspection')
    artists = np.array([str(item) for item in info[:, 0]])
    titles = np.array([str(item) for item in info[:, 1]])
    np.savez_compressed(destination, artists=artists, titles=titles, raw_labels=labels)
    save(base / 'annotation-acquisition.json', {'created_at': stamp(), 'source_commit': AMG_COMMIT,
         'mirror': 'https://github.com/loichan-tw/AMG1608_release', 'parts': artifacts,
         'zip_members': files, 'annotations_sha256': digest(destination),
         'ratings_shape': list(labels.shape), 'finite_ratings': int(finite.sum()),
         'individual_coordinates_outside_documented_range': int((np.abs(labels[finite]) > 1).sum()),
         'observed_min': float(labels[finite].min()), 'observed_max': float(labels[finite].max()),
         'policy': 'Verify every split archive against the pinned Git blob; load original song identities and individual VA ratings only. Preserve six slight coordinate overshoots outside [-1,1]; validate aggregated means independently. Supplied acoustic features are not training inputs.'})
    if remove_parts:
        for path in paths:
            path.unlink()


def native_annotations(dataset):
    if dataset == 'amg1608-matched':
        path = DATA / 'amg1608/mirror/annotations.npz'
        result = []
        with np.load(path) as source:
            for index, (artist, title, labels) in enumerate(zip(source['artists'], source['titles'], source['raw_labels'])):
                paired = np.isfinite(labels).all(1)
                if paired.sum() < 3:
                    continue
                if (np.abs(labels[paired].mean(0)) > 1).any():
                    raise ValueError('AMG song mean outside documented scale')
                result.append({'id': f'amg1608:{index + 1}', 'artist': str(artist), 'title': str(title),
                               'targets': ((labels[paired].mean(0) + 1) / 2).tolist(),
                               'raters': int(paired.sum()), 'rater_std_native': labels[paired].std(0).tolist()})
        return result, {'source_sha256': digest(path), 'native_scale': [-1, 1]}
    if dataset != 'ntwicm-matched':
        raise ValueError(dataset)
    path = DATA / 'ntwicm/NTWICM-Mood-Annotation.arff'
    text = path.read_text(encoding='cp1252').split('@data', 1)[1]
    result = []
    for index, fields in enumerate(csv.reader(io.StringIO(text.strip()), quotechar="'", escapechar='\\', skipinitialspace=True)):
        if len(fields) != 11:
            raise ValueError('NTWICM ARFF field-count mismatch')
        labels = np.array([float(value) for value in fields[2:10]]).reshape(4, 2)
        if not np.isin(labels, [-2, -1, 0, 1, 2]).all():
            raise ValueError('Unexpected NTWICM rating value')
        result.append({'id': f'ntwicm:{index + 1}', 'artist': fields[0], 'title': fields[1],
                       'targets': ((labels.mean(0) + 2) / 4).tolist(), 'raters': 4,
                       'rater_std_native': labels.std(0).tolist(), 'original_partition': fields[-1],
                       'raw_ratings': labels.tolist()})
    if len(result) != 2648:
        raise ValueError('Unexpected NTWICM annotation row count')
    return result, {'source_sha256': digest(path), 'native_scale': [-2, 2]}


def prepare(dataset):
    annotations, provenance = native_annotations(dataset)
    lookup = {}
    for row in annotations:
        for artist in artist_keys(row['artist']):
            lookup.setdefault((artist, identity(row['title'])), []).append(row)
    # Prefer supplied Coimbra excerpts; otherwise use other supplied recordings.
    # No fuzzy title matching, inferred identity, or prediction-score selection.
    sources = ('coimbra-mirex-like', '4q-emotion', 'cal500', 'pmemo')
    matches, ambiguous, source_hashes = {}, [], {}
    for source in sources:
        path = OUT / f'{source}.features.json'
        source_hashes[source] = digest(path)
        for audio in json.loads(path.read_text())['rows']:
            found = {row['id']: row for artist in artist_keys(audio.get('artist', ''))
                     for row in lookup.get((artist, identity(audio.get('title', ''))), [])}
            if not found:
                continue
            identities = {(identity(row['artist']), identity(row['title'])) for row in found.values()}
            if len(identities) != 1:
                ambiguous.append({'audio_id': audio['id'], 'annotation_ids': list(found)})
                continue
            key = next(iter(identities))
            if key in matches:
                matches[key]['alternate_audio_ids'].append(audio['id'])
                continue
            original = list(found.values())
            weights = np.array([row['raters'] for row in original])
            targets = np.average(np.array([row['targets'] for row in original]), axis=0, weights=weights)
            matches[key] = {**audio, 'id': dataset + ':' + original[0]['id'].split(':')[-1],
                            'group': dataset + ':artist:' + min(artist_keys(original[0]['artist'])),
                            'source_audio_dataset': source, 'source_audio_id': audio['id'],
                            'artist': original[0]['artist'], 'title': original[0]['title'],
                            'targets': targets.tolist(), 'target_names': ['valence', 'arousal'],
                            'annotation_rows': original, 'alternate_audio_ids': [],
                            'annotation_unit': 'whole_song_label_on_identity_matched_supplied_excerpt',
                            'native_scale': provenance['native_scale']}
    rows = list(matches.values())
    if len(rows) < 30:
        raise ValueError('Fewer than thirty unambiguous supplied-audio matches')
    save(OUT / f'{dataset}.features.json', {'created_at': stamp(), 'dataset': dataset, 'rows': rows, 'errors': [],
         'secondary_annotation_provenance': {**provenance, 'source_features_sha256': source_hashes}})
    save(OUT / f'{dataset}.join-audit.json', {'created_at': stamp(), 'rows': len(rows),
         'artist_groups': len({row['group'] for row in rows}), 'native_annotation_rows': len(annotations),
         'ambiguous': ambiguous, 'source_audio_counts': {source: sum(row['source_audio_dataset'] == source for row in rows) for source in sources},
         'source_features_sha256': source_hashes, **provenance,
         'policy': 'Exact Unicode-normalized artist/title identity; additionally permit explicit surname,given reversal. Aggregate repeated native rating rows; retain one deterministic supplied audio per identity.',
         'limitations': ['Artist/title identity does not prove the same master, edit or excerpt as the original annotation study.',
                         'This is weak song-level supervision on supplied excerpts, not the original dataset benchmark split.',
                         'Matched subset depends on local audio availability and is not representative of the full release.']})
    print(json.dumps({'secondary_annotation_dataset': dataset, 'rows': len(rows)}), flush=True)
    return rows
