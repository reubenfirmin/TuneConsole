"""Bounded, checksummed acquisition of human mid-level property annotations."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request
import zipfile

from ..support import quality_round as evidence
from ..support.external_experiment import LAB, digest, save
from ..focus.focus_experiment import inside, load

DEFAULT = LAB / 'local/midlevel-811-20261101/round-1'
DISCOVERY = LAB / 'local/datasets/discovery/research-20261101'
DOCUMENT = LAB / 'MIDLEVEL_PLAN.md'
EXPECTED = 'f0a36e901ed6d9e4f21d61d49cd2631642140e3329bb7e41c7524e09688fe1ac'
FILE_ID = '5b20bd6eba5d13000f092ab8'
URLS = [
    'https://osf.io/download/4v3xh/',
    'https://osf.io/4v3xh/download',
    f'https://files.osf.io/v1/resources/5aupt/providers/osfstorage/{FILE_ID}?action=download&direct=1',
    f'https://files.osf.io/v1/resources/5aupt/providers/osfstorage/{FILE_ID}/?action=download',
    f'https://files.osf.io/v1/resources/5aupt/providers/osfstorage/{FILE_ID}?action=download&version=1',
    'https://osf.io/5aupt/files/osfstorage/' + FILE_ID + '?action=download',
]


def freeze(directory):
    evidence.freeze(directory, 811, {'document_sha256': digest(DOCUMENT), 'urls': URLS,
        'expected_sha256': EXPECTED, 'expected_bytes': 318090,
        'metadata_seconds': 300, 'response_bytes': 2 * 1024**2, 'total_bytes': 16 * 1024**2,
        'repositories': ['shreyanc/midlevel_general', 'CPJKU/robustness_mer_bottleneck']},
        {n: DISCOVERY / n for n in ('midlevel-node.response', 'midlevel-files.response', 'midlevel-file-info.response')},
        documents=[DOCUMENT])
    print(json.dumps({'plan_sha256': digest(directory / 'plan.json')}), flush=True)


def acquire(directory):
    if (directory / 'acquisition.receipt.json').exists():
        print(json.dumps(evidence.verify(directory, required_stages=['acquisition'])))
        return
    started = time.monotonic()
    report = {'attempts': [], 'repositories': [], 'accepted_metadata': None,
              'outcome': 'acquisition-limited', 'human_property_rows_available': None}
    outputs = []
    total = 0
    with TemporaryDirectory(prefix='midlevel-download-', dir=directory) as temporary:
        for url in URLS:
            if time.monotonic() - started > 300:
                break
            tick = time.monotonic()
            record = {'url': url}
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'TuneConsole-research/1'})
                with urllib.request.urlopen(request, timeout=20) as response:
                    record.update(status=response.status, final_url=response.url, headers=dict(response.headers))
                    payload = response.read(2 * 1024**2 + 1)
                total += len(payload)
                if len(payload) > 2 * 1024**2 or total > 16 * 1024**2:
                    raise ValueError('Metadata byte bound exceeded')
                record.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                if len(payload) != 318090 or record['sha256'] != EXPECTED:
                    raise ValueError('Response is not the publisher-hashed annotation archive')
                archive = directory / 'metadata_annotations.zip'
                archive.write_bytes(payload)
                outputs.append(archive)
                with zipfile.ZipFile(archive) as zipped:
                    members = zipped.infolist()
                    if sum(m.file_size for m in members) > 16 * 1024**2:
                        raise ValueError('Metadata expansion bound exceeded')
                    report['archive_members'] = [{'name': m.filename, 'bytes': m.file_size} for m in members]
                    for member in members:
                        target = (directory / 'metadata' / member.filename).resolve()
                        if not target.is_relative_to(directory / 'metadata'):
                            raise ValueError('Unsafe archive path')
                        if not member.is_dir():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(zipped.read(member))
                            outputs.append(target)
                report.update(accepted_metadata=str(archive), outcome='metadata-ready-audit-before-pilot')
            except urllib.error.HTTPError as error:
                body = error.read(1024)
                record.update(status=error.code, error=str(error), error_body_sha256=hashlib.sha256(body).hexdigest(),
                              error_body_prefix=body.decode('utf-8', errors='replace'))
            except Exception as error:
                record.update(error=type(error).__name__ + ': ' + str(error))
            record['seconds'] = time.monotonic() - tick
            report['attempts'].append(record)
            save(directory / 'acquisition-progress.json', report)
            print(json.dumps({'attempt': url, 'status': record.get('status'), 'error': record.get('error')}), flush=True)
            if report['accepted_metadata']:
                break
        for repo in load(directory / 'plan.json')['protocol']['repositories']:
            if time.monotonic() - started > 300:
                break
            record = {'repository': repo}
            try:
                detail = json.loads(subprocess.check_output(['gh', 'api', 'repos/' + repo], text=True, timeout=20))
                branch = detail['default_branch']
                commit = json.loads(subprocess.check_output(['gh', 'api', 'repos/' + repo + '/commits/' + branch], text=True, timeout=20))['sha']
                tree = subprocess.check_output(['gh', 'api', '-X', 'GET', 'repos/' + repo + '/git/trees/' + commit,
                                                '-f', 'recursive=1'], timeout=20)
                total += len(tree)
                if total > 16 * 1024**2:
                    raise ValueError('Metadata byte bound exceeded')
                path = directory / (repo.replace('/', '-') + '-tree.json')
                path.write_bytes(tree)
                outputs.append(path)
                items = json.loads(tree)['tree']
                candidates = [r for r in items if r['type'] == 'blob' and
                    ('annotation' in r['path'].lower() or r['path'].endswith('metadata_annotations.zip'))]
                record.update(revision=commit, tree_sha256=digest(path), annotation_candidates=candidates,
                    license=(detail.get('license') or {}).get('spdx_id'))
                readme = next(r for r in items if r['path'] == 'README.md')
                blob = json.loads(subprocess.check_output(['gh', 'api', 'repos/' + repo + '/git/blobs/' + readme['sha']], text=True, timeout=20))
                payload = base64.b64decode(blob['content'])
                assert hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest() == readme['sha']
                path = directory / (repo.replace('/', '-') + '-README.md')
                path.write_bytes(payload)
                outputs.append(path)
                record['readme_sha256'] = digest(path)
            except Exception as error:
                record['error'] = type(error).__name__ + ': ' + str(error)
            report['repositories'].append(record)
    report.update(seconds=time.monotonic() - started, response_bytes=total,
        limits='Missing metadata means unknown usable overlap, not zero real overlap or a model-quality result.')
    save(directory / 'acquisition.json', report)
    evidence.seal(directory, 'acquisition', [directory / 'acquisition.json', *outputs])
    print(json.dumps({'outcome': report['outcome'], 'seconds': report['seconds']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'acquire', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    if args.action == 'freeze':
        freeze(directory)
    elif args.action == 'verify':
        print(json.dumps(evidence.verify(directory, required_stages=['acquisition'])))
    else:
        evidence.validate(directory, execution=True)
        acquire(directory)


if __name__ == '__main__':
    main()
