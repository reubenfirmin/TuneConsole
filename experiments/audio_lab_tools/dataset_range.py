"""Inspect published dataset ZIPs with bounded, checked HTTP range requests.

Acquire metadata first. This does not download a whole archive or claim to have
verified its whole-file checksum. ZIP CRC checks cover each extracted member.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile

from .external_experiment import DATA, digest, save, stamp


class RemoteZip(io.RawIOBase):
    def __init__(self, url, size, target, *, budget=32 * 1024 * 1024):
        self.url, self.size, self.target, self.budget = url, size, target, budget
        self.position, self.received = 0, 0
        self.requested = 0
        self.ranges = []

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        position = offset if whence == io.SEEK_SET else self.position + offset if whence == io.SEEK_CUR else self.size + offset
        if position < 0 or position > self.size:
            raise ValueError('ZIP seek outside published archive')
        self.position = position
        return position

    def read(self, length=-1):
        length = self.size - self.position if length < 0 else min(length, self.size - self.position)
        if length == 0:
            return b''
        if length > self.budget:
            raise ValueError('Individual ZIP read exceeds acquisition budget')
        start, end = self.position, self.position + length - 1
        path = self.target / 'ranges' / f'{start}-{end}.bin'
        if path.exists() and path.with_suffix('.json').exists():
            metadata = json.loads(path.with_suffix('.json').read_text())
            if metadata['url'] != self.url or path.stat().st_size != length or digest(path) != metadata['sha256']:
                raise ValueError('Cached archive range changed')
            data = path.read_bytes()
        else:
            request = urllib.request.Request(self.url, headers={'Range': f'bytes={start}-{end}',
                'Accept-Encoding': 'identity', 'User-Agent': 'TuneConsole-dataset-research/1'})
            for attempt in range(3):
                if self.requested + length > self.budget:
                    raise ValueError('Acquisition request-byte budget reached')
                self.requested += length
                try:
                    with urllib.request.urlopen(request, timeout=45) as response:
                        expected = f'bytes {start}-{end}/{self.size}'
                        if response.status != 206 or response.headers.get('Content-Range') != expected:
                            raise ValueError('Server did not honor exact archive range: ' + repr(response.headers.get('Content-Range')))
                        data = response.read(length + 1)
                    if len(data) != length:
                        raise ConnectionError('Incomplete archive range')
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                    if (isinstance(error, urllib.error.HTTPError) and error.code not in (408, 429, 500, 502, 503, 504)) or attempt == 2:
                        raise
                    time.sleep(attempt + 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            save(path.with_suffix('.json'), {'url': self.url, 'sha256': digest(path), 'bytes': length})
            self.received += length
        self.ranges.append({'start': start, 'end': end, 'sha256': digest(path)})
        self.position += length
        return data


def inspect(dataset):
    discovery = DATA / 'discovery'
    target = discovery / dataset
    target.mkdir(parents=True, exist_ok=True)
    if dataset == 'manymusic':
        source = discovery / 'manymusic-zenodo.json'
        metadata = json.loads(source.read_text())
        item = next(row for row in metadata['files'] if row['key'].endswith('.zip'))
        url, size, checksum = item['links']['self'], item['size'], item['checksum']
    else:
        source = discovery / 'memo2496-figshare.json'
        metadata = json.loads(source.read_text())
        item = next(row for row in metadata['files'] if row['name'] == 'MusicRawData.zip')
        url, size, checksum = item['download_url'], item['size'], item['computed_md5']
    reader = RemoteZip(url, size, target)
    with zipfile.ZipFile(reader) as archive:
        members = [{'name': row.filename, 'bytes': row.file_size, 'compressed_bytes': row.compress_size,
                    'crc32': f'{row.CRC:08x}'} for row in archive.infolist()]
        save(target / 'archive-members.json', {'url': url, 'published_bytes': size,
            'published_checksum_not_whole_file_verified': checksum, 'metadata_sha256': digest(source),
            'members': members})
        extracted = []
        for member in members:
            name = Path(member['name'])
            if name.is_absolute() or '..' in name.parts:
                raise ValueError('Unsafe archive member')
            if '__MACOSX' in name.parts:
                continue
            if name.suffix.lower() not in ('.csv', '.txt', '.json', '.md') or member['bytes'] > 8 * 1024 * 1024:
                continue
            data = archive.read(member['name'])
            path = target / 'metadata' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            extracted.append({**member, 'sha256': digest(path)})
    report = {'created_at': stamp(), 'archive_members': len(members), 'extracted_metadata': extracted,
              'new_network_bytes': reader.received, 'ranges': reader.ranges,
              'limitations': 'Partial range acquisition; whole-archive MD5 is not established. Exact response ranges, cached SHA256 and each extracted ZIP member CRC are checked.'}
    save(target / 'inspection.json', report)
    print(json.dumps({'dataset': dataset, 'archive_members': len(members),
                      'metadata_files': [row['name'] for row in extracted],
                      'network_bytes': reader.received}), flush=True)
    return report


def rated_audio():
    """Acquire only supplied ManyMusic recordings with actual human VA ratings.

    The preflight bound covers all requested compressed audio, not just one
    worker. Each member is CRC checked by zipfile and SHA256 pinned on disk.
    Model predictions, curation votes and prompt emotion words are not labels.
    """
    target = DATA / 'discovery/manymusic'
    source = target.parent / 'manymusic-zenodo.json'
    release = json.loads(source.read_text())
    item = next(row for row in release['files'] if row['key'].endswith('.zip'))
    url, size = item['links']['self'], item['size']
    ratings_path = target / 'metadata/manymusic-stim-zenodo/validation-dataset/ratings/ratings.csv'
    ratings = list(csv.DictReader(ratings_path.open()))
    members = json.loads((target / 'archive-members.json').read_text())['members']
    by_id = {Path(row['name']).stem: row for row in members
             if row['name'].endswith('.mp3') and '__MACOSX' not in row['name']}
    selected = sorted({row['trackId'] for row in ratings} & by_id.keys())
    compressed_bytes = sum(by_id[key]['compressed_bytes'] for key in selected)
    if compressed_bytes > 3 * 1024 ** 3:
        raise ValueError('Rated audio exceeds the three GiB acquisition bound')
    save(target / 'rated-audio-selection.json', {
        'created_at': stamp(), 'release_metadata_sha256': digest(source),
        'ratings_sha256': digest(ratings_path), 'tracks': selected,
        'compressed_bytes': compressed_bytes,
        'policy': 'Exact supplied MP3 filename intersection with human ratings; retain source and per-rating start/play timestamps. Missing AppleMusic/Spotify audio is excluded.'})

    def acquire(key):
        member = by_id[key]
        path = target / 'rated-audio' / (key + '.mp3')
        record_path = path.with_suffix('.json')
        if path.exists() and record_path.exists():
            record = json.loads(record_path.read_text())
            if (record['archive_member'] != member or record['archive_url'] != url
                    or path.stat().st_size != member['bytes'] or digest(path) != record['sha256']):
                raise ValueError('Rated audio cache changed: ' + key)
            return record
        reader = RemoteZip(url, size, target, budget=64 * 1024 * 1024)
        with zipfile.ZipFile(reader) as archive:
            data = archive.read(member['name'])
        if len(data) != member['bytes']:
            raise ValueError('Wrong audio size: ' + key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        record = {'id': key, 'archive_url': url, 'archive_member': member,
                  'sha256': digest(path), 'new_network_bytes': reader.received,
                  'crc32_verified': True, 'ranges': reader.ranges}
        save(record_path, record)
        print(json.dumps({'rated_audio': key, 'bytes': len(data)}), flush=True)
        return record

    # These are independent network transfers. Heavy extraction still uses the
    # repository's single test/build lock in the separate evaluation runner.
    with ThreadPoolExecutor(max_workers=4) as workers:
        records = list(workers.map(acquire, selected))
    report = {'created_at': stamp(), 'tracks': len(records), 'files': records,
              'selection_sha256': digest(target / 'rated-audio-selection.json'),
              'limitations': 'Whole archive MD5 unverified; acquired members pass ZIP CRC and local SHA256. Human annotations refer to their recorded excerpt positions, not every section of these full recordings.'}
    save(target / 'rated-audio-acquisition.json', report)
    print(json.dumps({'rated_audio_complete': len(records), 'compressed_bytes': compressed_bytes}), flush=True)
    return report


def audio_licenses():
    """Retain the released per-track provenance omitted by the small-file audit."""
    target = DATA / 'discovery/manymusic'
    release_path = target.parent / 'manymusic-zenodo.json'
    release = json.loads(release_path.read_text())
    item = next(row for row in release['files'] if row['key'].endswith('.zip'))
    members = json.loads((target / 'archive-members.json').read_text())['members']
    member = next(row for row in members if Path(row['name']).name == 'audio_licenses.txt'
                  and '__MACOSX' not in Path(row['name']).parts)
    if member['bytes'] > 16 * 1024 * 1024 or member['compressed_bytes'] > 16 * 1024 * 1024:
        raise ValueError('Audio provenance member exceeds the bounded metadata request')
    reader = RemoteZip(item['links']['self'], item['size'], target)
    with zipfile.ZipFile(reader) as archive:
        data = archive.read(member['name'])
    if len(data) != member['bytes']:
        raise ValueError('Incomplete per-track provenance member')
    destination = target / 'metadata' / member['name']
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    report = {'created_at': stamp(), 'archive_url': item['links']['self'], 'archive_member': member,
        'sha256': digest(destination), 'crc32_verified': True,
        'new_network_bytes': reader.received, 'ranges': reader.ranges,
        'scope': 'Publisher-supplied per-track provenance; retaining it does not resolve every source condition or establish product adoption.'}
    save(target / 'audio-provenance-acquisition.json', report)
    print(json.dumps({'manymusic_audio_provenance_bytes': len(data), 'sha256': report['sha256'],
                      'new_network_bytes': reader.received}), flush=True)
    return report


def memo_pilot(count):
    """Pin an audio pilot by filename hash before examining emotion labels."""
    if not 1 <= count <= 128:
        raise ValueError('Audio pilot must contain between one and 128 tracks')
    target = DATA / 'discovery/memo2496'
    source = target.parent / 'memo2496-figshare.json'
    metadata = json.loads(source.read_text())
    item = next(row for row in metadata['files'] if row['name'] == 'MusicRawData.zip')
    url, size = item['download_url'], item['size']
    members = json.loads((target / 'archive-members.json').read_text())['members']
    members = sorted((row for row in members if row['name'].endswith('.mp3')),
        key=lambda row: hashlib.sha256(('memo-audio-pilot-1/' + row['name']).encode()).hexdigest())[:count]
    if sum(row['compressed_bytes'] for row in members) > 768 * 1024 ** 2:
        raise ValueError('Pilot exceeds the 768 MiB acquisition bound')
    save(target / 'pilot-selection.json', {
        'created_at': stamp(), 'release_metadata_sha256': digest(source), 'members': members,
        'policy': 'First N ascending SHA256(memo-audio-pilot-1/filename). No target, source, duration or observed-model selection. A pipeline pilot, not an artist-independent benchmark.'})

    def acquire(member):
        name = Path(member['name'])
        if name.name != member['name']:
            raise ValueError('Unexpected Memo2496 audio path')
        path = target / 'pilot-audio' / name
        record_path = path.with_suffix('.json')
        if path.exists() and record_path.exists():
            record = json.loads(record_path.read_text())
            if record['archive_member'] != member or record['archive_url'] != url or digest(path) != record['sha256']:
                raise ValueError('Pilot cache changed')
            return record
        reader = RemoteZip(url, size, target, budget=32 * 1024 * 1024)
        with zipfile.ZipFile(reader) as archive:
            data = archive.read(member['name'])
        if len(data) != member['bytes']:
            raise ValueError('Incomplete pilot member')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        record = {'archive_url': url, 'archive_member': member, 'sha256': digest(path),
                  'crc32_verified': True, 'new_network_bytes': reader.received, 'ranges': reader.ranges}
        save(record_path, record)
        print(json.dumps({'memo_pilot_audio': name.name, 'bytes': len(data)}), flush=True)
        return record

    with ThreadPoolExecutor(max_workers=4) as workers:
        records = list(workers.map(acquire, members))
    report = {'created_at': stamp(), 'files': records,
              'selection_sha256': digest(target / 'pilot-selection.json'),
              'limitations': 'Partial acquisition with member CRC/SHA256 checks, not a whole-archive MD5 check. Artist metadata was removed by the authors, so artist independence remains unverified.'}
    save(target / 'pilot-acquisition.json', report)
    print(json.dumps({'memo_pilot_complete': len(records)}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', choices=['manymusic', 'memo2496'])
    parser.add_argument('--rated-audio', action='store_true')
    parser.add_argument('--audio-licenses', action='store_true')
    parser.add_argument('--audio-pilot', type=int)
    args = parser.parse_args()
    if sum((args.rated_audio, args.audio_licenses, args.audio_pilot is not None)) > 1:
        parser.error('Choose one acquisition mode')
    if args.audio_licenses:
        if args.dataset != 'manymusic':
            parser.error('--audio-licenses is specific to ManyMusic')
        audio_licenses()
    elif args.audio_pilot is not None:
        if args.dataset != 'memo2496':
            parser.error('--audio-pilot is specific to Memo2496')
        memo_pilot(args.audio_pilot)
    elif args.rated_audio:
        if args.dataset != 'manymusic':
            parser.error('--rated-audio is specific to ManyMusic')
        rated_audio()
    else:
        inspect(args.dataset)
