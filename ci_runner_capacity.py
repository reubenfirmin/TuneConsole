"""Measure the current unit runners before agreeing on whole-file ownership."""
import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import time
import urllib.request

WORKLOAD = 'sqlite-json-labels-v1'


def identity():
    return {key: os.environ[name] for key, name in (
        ('run_id', 'GITHUB_RUN_ID'), ('attempt', 'GITHUB_RUN_ATTEMPT'), ('revision', 'GITHUB_SHA'))}


def probe(index, total):
    """Fixed work, repeated for noise resistance; no application state or tests."""
    if not 1 <= index <= total:
        raise ValueError('Invalid capacity shard index')
    payload = json.dumps([{'title': f'Track {n}: A Long Release', 'artist': f'Artist {n}'}
                          for n in range(8)])
    samples = []
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, payload TEXT)')
        conn.executemany('INSERT INTO items VALUES (?, ?)', ((i, payload) for i in range(512)))
        normalize = re.compile(r'[^a-z0-9]+')
        for _ in range(3):
            started, cpu_started, checksum = time.perf_counter(), time.process_time(), 0
            for i in range(25000):
                rows = json.loads(conn.execute('SELECT payload FROM items WHERE id=?', (i % 512,)).fetchone()[0])
                checksum += sum(len(normalize.sub(' ', row['title'].lower() + row['artist'].lower())) for row in rows)
            samples.append({'seconds': time.perf_counter() - started,
                            'cpu_seconds': time.process_time() - cpu_started, 'checksum': checksum})
    return {**identity(), 'index': index, 'total': total, 'workload': WORKLOAD,
            'python': platform.python_version(), 'samples': samples,
            'seconds': statistics.median(sample['seconds'] for sample in samples)}


def load_capacities(directory, total):
    rows = sorted((json.loads(path.read_text()) for path in Path(directory).glob('*.json')),
                  key=lambda row: row['index'])
    if len(rows) != total or [row['index'] for row in rows] != list(range(1, total + 1)):
        raise ValueError('Every current runner capacity is required exactly once')
    expected = identity()
    for row in rows:
        if (any(row.get(key) != value for key, value in expected.items())
                or row.get('total') != total or row.get('workload') != WORKLOAD
                or row.get('python') != platform.python_version()):
            raise ValueError('Runner capacity source or runtime differs')
        seconds = row.get('seconds')
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('Invalid runner capacity measurement')
        samples = row.get('samples', [])
        if (len(samples) != 3 or any(type(sample.get('seconds')) not in (int, float)
                                    or not math.isfinite(sample['seconds']) or sample['seconds'] <= 0
                                    or sample.get('checksum') != 6000000 for sample in samples)
                or seconds != statistics.median(sample['seconds'] for sample in samples)):
            raise ValueError('Incomplete runner capacity samples')
    middle = statistics.median(row['seconds'] for row in rows)
    return [middle / row['seconds'] for row in rows], rows


def artifact_ids(fetch, total, attempt, *, now=time.monotonic, sleep=time.sleep, seconds=180):
    """Wait only for immutable manifests from this attempt, before test execution."""
    if total < 1:
        raise ValueError('A positive runner count is required')
    names = [f'unit-capacity-{index}-{attempt}' for index in range(1, total + 1)]
    deadline = now() + seconds
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            raise TimeoutError('Current unit runner capacities did not all arrive before test execution')
        matches = {}
        for artifact in fetch(min(10, remaining)):
            name = artifact['name']
            if name in names and not artifact.get('expired'):
                if name in matches:
                    raise ValueError('Duplicate current runner capacity artifact')
                if type(artifact.get('id')) is not int or artifact['id'] < 1:
                    raise ValueError('Invalid runner capacity artifact identity')
                matches[name] = str(artifact['id'])
        if len(matches) == total and now() < deadline:
            return ','.join(matches[name] for name in names)
        sleep(min(3, max(0, deadline - now())))


def artifacts(timeout):
    scope = identity()
    repository = os.environ['GITHUB_REPOSITORY']
    deadline, result, page = time.monotonic() + timeout, [], 1
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Runner capacity listing exceeded its request budget')
        request = urllib.request.Request(
            f'https://api.github.com/repos/{repository}/actions/runs/{scope["run_id"]}/artifacts?per_page=100&page={page}',
            headers={'Authorization': f'Bearer {os.environ["GITHUB_TOKEN"]}',
                     'Accept': 'application/vnd.github+json', 'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(request, timeout=remaining) as response:
            listing = json.load(response)
        result.extend(listing['artifacts'])
        if len(result) >= listing['total_count']:
            return result
        if not listing['artifacts']:
            raise ValueError('Incomplete runner capacity artifact listing')
        page += 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('probe', 'wait'))
    parser.add_argument('--index', type=int)
    parser.add_argument('--total', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'probe' and args.index is None:
        parser.error('probe requires --index')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.action == 'probe':
        args.output.write_text(json.dumps(probe(args.index, args.total), indent=2) + '\n')
    else:
        ids = artifact_ids(artifacts, args.total, identity()['attempt'])
        with args.output.open('a') as stream:
            stream.write(f'artifacts={ids}\n')
