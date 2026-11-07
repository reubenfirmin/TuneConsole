#!/usr/bin/env python3
"""Standalone proof of image/snapshot first-write sharing.

This copies SQLite once, then tests small real SQLite writes and representative
mutable files in concurrent snapshots. The shared manager in tests.library_image
captures the complete file closure and serves actual-library fixtures.
"""
import argparse
from contextlib import closing
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
IMAGE_ROOT = ROOT / 'build/library-image-poc'
CONTAINER = '61d4bdcb63596f3530ad7a3594217c79a237128025c0a9da2fdb5b256b1c8dc1'


def command(*args):
    result = subprocess.run([str(arg) for arg in args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr.strip()}')
    return result.stdout.strip()


def prepare():
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    image = IMAGE_ROOT / 'storage.img'
    with image.open('xb') as stream:
        stream.truncate(4 * 1024**3)
    command('mkfs.btrfs', '--nodiscard', image)
    (IMAGE_ROOT / 'volume').mkdir(exist_ok=False)
    print(json.dumps(dict(image=str(image), virtual_bytes=image.stat().st_size,
                          allocated_bytes=image.stat().st_blocks * 512,
                          next_step='Run scripts/test-library-image-poc-mount.sh as root'), indent=2))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def worker(name, hidden_source):
    root = Path('/library')
    assert not Path(hidden_source).exists(), 'Live library is visible inside the run'
    with (root / '.readers.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        connection = None
        try:
            print(json.dumps({'ready': True}), flush=True)
            for line in sys.stdin:
                operation = line.strip()
                if operation == 'exit':
                    return
                if operation == 'mutate':
                    connection = sqlite3.connect(root / 'state.db')
                    connection.execute('PRAGMA journal_mode=WAL')
                    connection.execute('PRAGMA wal_autocheckpoint=0')
                    with connection:
                        connection.execute('UPDATE image_poc SET value=?', (name,))
                        connection.execute('CREATE TABLE image_poc_migration(value TEXT)')
                        connection.execute('INSERT INTO image_poc_migration VALUES (?)', (name,))
                    assert connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
                    replacement = root / 'replacement.tmp'
                    replacement.write_text(name)
                    os.replace(replacement, root / 'replace.txt')
                    (root / 'delete.txt').unlink()
                    (root / '.budget').write_text(name)
                    for filename in ('replace.txt', '.budget'):
                        with (root / filename).open('rb') as stream:
                            os.fsync(stream.fileno())
                else:
                    assert operation == 'inspect', operation
                with closing(sqlite3.connect((root / 'state.db').as_uri() + '?mode=ro', uri=True)) as reader:
                    value, logical = reader.execute('SELECT value,file_path FROM image_poc').fetchone()
                    result = dict(value=value, logical_path=logical,
                        file=Path(logical).read_text(), deleted=not (root / 'delete.txt').exists(),
                        budget=(root / '.budget').read_text(), sqlite=sqlite3.sqlite_version,
                        python=sys.version, wal=(root / 'state.db-wal').exists(),
                        shm=(root / 'state.db-shm').exists())
                    migrated = reader.execute("SELECT 1 FROM sqlite_schema WHERE name='image_poc_migration'").fetchone()
                    result['migration'] = reader.execute('SELECT value FROM image_poc_migration').fetchone()[0] if migrated else None
                print(json.dumps(result), flush=True)
        finally:
            if connection is not None:
                connection.close()


class Client:
    def __init__(self, directory, source, image, label):
        self.name = 'tuneconsole-image-poc-' + uuid.uuid4().hex
        self.booted_at = time.monotonic()
        self.ready = False
        self.process = subprocess.Popen([
            'podman', 'run', '--rm', '-i', '--name', self.name, '--pull=never',
            '--userns=keep-id', '--network=none', '--read-only', '--cap-drop=all',
            '--security-opt=label=disable', '--memory=512m', '--pids-limit=64',
            '--tmpfs', '/tmp:rw,size=16m,nodev,nosuid',
            '--volume', str(directory) + ':/library:rw',
            '--volume', str(Path(__file__).resolve()) + ':/image-poc.py:ro',
            '--entrypoint', 'python3', image, '-u', '/image-poc.py',
            '--worker', label, '--hidden-source', str(source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def request(self, operation):
        if not self.ready:
            assert self.receive() == {'ready': True}
            self.ready = True
        self.process.stdin.write(operation + '\n')
        self.process.stdin.flush()
        return self.receive()

    def receive(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                state = command('podman', 'inspect', '--format', '{{json .State}}', self.name)
                raise TimeoutError('PoC container did not answer: ' + self.name + ' ' + state)
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(self.process.stderr.read())
        return json.loads(line)

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.communicate(input='exit\n', timeout=5)
            else:
                self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Stop only this PoC's named container before removing its subvolume.
            # The normal protocol exits cleanly; a stuck worker is killed at once.
            subprocess.run(['podman', 'rm', '--force', '--ignore', '--time', '0', self.name],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.process.communicate(timeout=15)
        assert self.process.poll() is not None


def allocation(volume, image, directories):
    command('btrfs', 'filesystem', 'sync', volume)
    database_extents = {}
    for path in directories:
        output = command('btrfs', 'filesystem', 'du', '--raw', '-s', path / 'state.db')
        total, exclusive, shared, _ = output.splitlines()[-1].split(maxsplit=3)
        database_extents[path.name] = dict(total_bytes=int(total), exclusive_bytes=int(exclusive),
                                           shared_bytes=int(shared))
    return dict(image_allocated_bytes=image.stat().st_blocks * 512,
                database_extents=database_extents,
                extents={str(path.name): command('btrfs', 'filesystem', 'du', '--raw', '-s', path)
                         for path in directories})


def run(args):
    source, volume = args.source.resolve(), args.volume.resolve()
    image = volume.parent / 'storage.img'
    assert command('stat', '-f', '-c', '%T', volume) == 'btrfs', 'Mount the PoC image first'
    assert command('podman', 'info', '--format', '{{.Host.Security.Rootless}}') == 'true'
    command('podman', 'image', 'exists', args.container_image)
    report = dict(success=False, source=str(source), phases={}, acquisitions=[], observations=[],
        scope='SQLite backend and representative mutable files; application file closure not captured')
    clients, subvolumes = [], []
    with tempfile.TemporaryDirectory(prefix='proof-', dir=volume) as temporary:
        root = Path(temporary)
        baseline = root / 'baseline'
        try:
            started = time.monotonic()
            command('btrfs', 'subvolume', 'create', baseline)
            subvolumes.append(baseline)
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as donor:
                donor.execute('PRAGMA query_only=ON')
                with closing(sqlite3.connect(baseline / 'state.db')) as target, target:
                    donor.backup(target)
                    target.execute('CREATE TABLE image_poc(value TEXT,file_path TEXT)')
                    target.execute("INSERT INTO image_poc VALUES ('baseline','/library/replace.txt')")
                report['donor_total_changes'] = donor.total_changes
            for filename in ('replace.txt', 'delete.txt', '.budget'):
                (baseline / filename).write_text('baseline')
            (baseline / '.readers.lock').touch()
            command('btrfs', 'property', 'set', '-ts', baseline, 'ro', 'true')
            report['phases']['baseline_seconds'] = time.monotonic() - started
            report['database_bytes'] = (baseline / 'state.db').stat().st_size
            report['baseline_sha256_before'] = digest(baseline / 'state.db')
            try:
                (baseline / 'replace.txt').write_text('forbidden')
            except OSError as error:
                assert error.errno == errno.EROFS
            else:
                raise AssertionError('Baseline was writable')

            branches = []
            for label in ('a', 'b'):
                branch = root / label
                started = time.monotonic()
                command('btrfs', 'subvolume', 'snapshot', baseline, branch)
                report['acquisitions'].append(dict(label=label, seconds=time.monotonic()-started))
                branches.append(branch)
                subvolumes.append(branch)
            report['before_writes'] = allocation(volume, image, branches)
            with (baseline / '.readers.lock').open('rb') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for label, branch in zip(('a', 'b'), branches):
                    clients.append(Client(branch, source, args.container_image, label))
                for client in clients:
                    result = client.request('inspect')
                    assert result['value'] == result['file'] == result['budget'] == 'baseline'
                    result['boot_seconds'] = time.monotonic()-client.booted_at
                    report['observations'].append(result)
                a = clients[0].request('mutate')
                b = clients[1].request('inspect')
                assert a['value'] == a['file'] == a['budget'] == 'a' and a['deleted']
                assert a['wal'] and a['shm'] and a['migration'] == 'a'
                assert b['value'] == b['file'] == b['budget'] == 'baseline' and not b['deleted']
                assert b['migration'] is None
                report['after_first_write'] = allocation(volume, image, branches)
                growth = (report['after_first_write']['image_allocated_bytes']
                          - report['before_writes']['image_allocated_bytes'])
                report['first_write_image_growth_bytes'] = growth
                assert growth < max(16 * 1024**2, report['database_bytes'] // 20), 'First write copied a substantial part of SQLite'
                assert report['after_first_write']['database_extents']['a']['exclusive_bytes'] < max(
                    16 * 1024**2, report['database_bytes'] // 20), 'Database extents were copied on first write'
                b = clients[1].request('mutate')
                a = clients[0].request('inspect')
                assert b['value'] == b['file'] == b['budget'] == 'b' and b['deleted']
                assert a['value'] == a['file'] == a['budget'] == 'a' and a['deleted']
                assert a['migration'] == 'a' and b['migration'] == 'b'
                report['observations'].extend([a, b])
                report['after_both_writes'] = allocation(volume, image, branches)
            for client in clients:
                client.close()
            clients.clear()
            report['baseline_sha256_after'] = digest(baseline / 'state.db')
            assert report['baseline_sha256_after'] == report['baseline_sha256_before']
            assert (baseline / 'delete.txt').exists()
            assert (baseline / 'replace.txt').read_text() == (baseline / '.budget').read_text() == 'baseline'
            report['success'] = True
        except BaseException as error:
            report['error'] = type(error).__name__ + ': ' + str(error)
            raise
        finally:
            try:
                for client in clients:
                    client.close()
                for subvolume in reversed(subvolumes):
                    # The frozen baseline is thawed only after all run processes
                    # have stopped, immediately before its final deletion.
                    command('btrfs', 'property', 'set', '-ts', subvolume, 'ro', 'false')
                    command('btrfs', 'subvolume', 'delete', subvolume)
                report['workspaces_removed'] = True
            except BaseException as error:
                report['success'] = False
                report['cleanup_error'] = type(error).__name__ + ': ' + str(error)
                raise
            finally:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(success=report['success'], report=str(args.report)), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-image', action='store_true')
    parser.add_argument('--source', type=Path, default=Path.home() / '.local/share/yt-playlist/state.db')
    parser.add_argument('--volume', type=Path, default=IMAGE_ROOT / 'volume')
    parser.add_argument('--container-image', default=CONTAINER)
    parser.add_argument('--report', type=Path, default=ROOT / 'build/test-reports/1166-image-poc.json')
    parser.add_argument('--worker', choices=('a', 'b'))
    parser.add_argument('--hidden-source')
    args = parser.parse_args()
    if args.prepare_image:
        prepare()
    elif args.worker:
        worker(args.worker, args.hidden_source)
    else:
        run(args)


if __name__ == '__main__':
    main()
