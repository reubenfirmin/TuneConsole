"""Explicit actual-library acceptance; never collected by ordinary pytest/CI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from .capture import verify_tree
from .manager import ROOT, command, save

WORKER = '''
from contextlib import closing
import fcntl, json, os, sqlite3, sys
from pathlib import Path
from yt_playlist.core.store import Store
root = Path('/library')
assert not Path(sys.argv[2]).exists(), 'Donor is visible'
assert not list(Path('/worktree/build').iterdir()), 'Other workspaces are visible'
store = Store(root / 'state.db')
store._bind_repositories()
assert store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] > 10000
files = sorted((root / 'state.db.evidence').glob('*/*/*.json'))
assert len(files) > 1, 'Actual evidence was not captured'
original = files[0].read_bytes()
original_budget = (root / 'state.db.evidence/.budget').read_bytes()
lock = (root / '.probe.lock').open('a+b')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
print(json.dumps({'ready': True, 'python': sys.version, 'sqlite': sqlite3.sqlite_version}), flush=True)
for line in sys.stdin:
    operation = line.strip()
    if operation == 'exit':
        break
    if operation == 'mutate':
        store.conn.execute('PRAGMA journal_mode=WAL')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        store.settings.set_setting('library_image_probe', sys.argv[1])
        with store.conn:
            store.conn.execute('CREATE TABLE image_probe_migration(value TEXT)')
            store.conn.execute('INSERT INTO image_probe_migration VALUES (?)', (sys.argv[1],))
        assert tuple(store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()) == (0, 0, 0)
        pending = root / 'replacement'
        pending.write_text(sys.argv[1])
        os.replace(pending, files[0])
        files[1].unlink()
        (root / 'state.db.evidence/.budget').write_text(sys.argv[1])
    else:
        assert operation == 'inspect'
    value = store.settings.get_setting('library_image_probe')
    print(json.dumps(dict(value=value, original_file=files[0].read_bytes() == original,
        second_exists=files[1].exists(), original_budget=(root / 'state.db.evidence/.budget').read_bytes() == original_budget,
        wal=(root / 'state.db-wal').exists(), shm=(root / 'state.db-shm').exists(),
        migration=store.conn.execute("SELECT name FROM sqlite_schema WHERE name='image_probe_migration'").fetchone() is not None)), flush=True)
store.conn.close()
lock.close()
'''


class Client:
    def __init__(self, manager, run, label):
        self.manager, self.run = manager, run
        self.started = time.monotonic()
        arguments = manager.container_args(run['path'], run['report'], run['name'])
        arguments.insert(2, '--interactive')
        self.child = subprocess.Popen(arguments + ['-u', '-c', WORKER, label, run['baseline']['source']],
                                      pass_fds=run['fds'], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def receive(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.child.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                raise TimeoutError('Library acceptance worker did not answer')
        line = self.child.stdout.readline()
        if not line:
            raise RuntimeError(self.child.stderr.read())
        return json.loads(line)

    def request(self, operation):
        self.child.stdin.write(operation + '\n')
        self.child.stdin.flush()
        return self.receive()

    def close(self):
        try:
            self.child.communicate(input='exit\n' if self.child.poll() is None else None, timeout=5)
        except subprocess.TimeoutExpired:
            self.manager.stop(self.run['name'])
            self.child.communicate(timeout=20)


def allocation(manager, runs):
    command('btrfs', 'filesystem', 'sync', manager.volume)
    result = {}
    for run in runs:
        row = command('btrfs', 'filesystem', 'du', '--raw', '-s', run['path'] / 'state.db').stdout.splitlines()[-1]
        total, exclusive, shared, _ = row.split(maxsplit=3)
        result[run['name']] = dict(total=int(total), exclusive=int(exclusive), shared=int(shared))
    backing = manager.volume.parent / 'storage.img'
    return dict(databases=result, backing_allocated_bytes=backing.stat().st_blocks * 512)


def interrupted_run(manager, baseline, report, signum):
    destination = report / signal.Signals(signum).name
    args = [sys.executable, str(ROOT / 'scripts/test-library.py'), '--volume', str(manager.volume),
            '--image', manager.image, 'run', '--baseline', baseline, '--report', str(destination), '--',
            'python3', '-u', '-c', 'import time; print("ready", flush=True); time.sleep(60)']
    with (report / (destination.name + '.log')).open('wb') as output:
        child = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                log = destination / 'output.log'
                if log.exists() and 'ready' in log.read_text():
                    break
                if child.poll() is not None:
                    raise RuntimeError((report / (destination.name + '.log')).read_text())
                time.sleep(.05)
            else:
                raise TimeoutError('Interrupted-run worker did not start')
            receipt = json.loads((destination / 'workspace.json').read_text())
            child.send_signal(signum)
            code = child.wait(timeout=20)
            path = manager.volume / 'workspaces' / receipt['id']
            if signum == signal.SIGKILL:
                # Exercise the documented stable cleanup command, including its
                # preview and the orphan's still-running container/CLI leases.
                cleanup = ROOT / 'scripts/test-artifacts-cleanup.py'
                preview = command(cleanup, path).stdout
                deadline = time.monotonic() + 5
                while True:
                    removed = command(cleanup, '--delete', path, check=False)
                    if removed.returncode == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(removed.stdout + removed.stderr)
                    time.sleep(.05)
            else:
                preview = None
            assert not path.exists()
            assert command('podman', 'container', 'exists', receipt['container'], check=False).returncode == 1
            return dict(signal=destination.name, returncode=code, preview=preview, cleaned=True)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if destination.joinpath('workspace.json').exists():
                identity = json.loads((destination / 'workspace.json').read_text())['id']
                if (manager.volume / 'runs' / (identity + '.json')).exists():
                    manager.collect(workspace=identity, delete=True)


def verify(manager, report, *, refresh=False):
    report.mkdir(parents=True, exist_ok=True)
    baseline = manager.resolve()
    result = dict(baseline=baseline['id'], success=False)
    try:
        with ThreadPoolExecutor(2) as pool:
            reused = list(pool.map(lambda _: manager.prepare(baseline['source']), range(2)))
        assert {item['id'] for item in reused} == {baseline['id']}
        with ExitStack() as stack:
            runs = [stack.enter_context(manager.workspace(baseline=baseline['id'], report=report / label))
                    for label in ('a', 'b')]
            clients = []
            for run, label in zip(runs, ('a', 'b')):
                client = Client(manager, run, label)
                clients.append(client)
                stack.callback(client.close)
            result['workers'] = [client.receive() for client in clients]
            result['acquisitions_seconds'] = [run['receipt']['acquisition_seconds'] for run in runs]
            result['startup_seconds'] = [time.monotonic() - client.started for client in clients]
            result['before'] = allocation(manager, runs)
            first = clients[0].request('mutate')
            other = clients[1].request('inspect')
            assert first['value'] == 'a' and first['migration'] and first['wal'] and first['shm']
            assert not first['original_file'] and not first['second_exists'] and not first['original_budget']
            assert other['value'] is None and not other['migration']
            assert other['original_file'] and other['second_exists'] and other['original_budget']
            result['after_first_write'] = allocation(manager, runs)
            exclusive = result['after_first_write']['databases'][runs[0]['name']]['exclusive']
            assert exclusive < 16 * 1024**2, 'First SQL write copied the database'
            result['observations'] = [first, other, clients[1].request('mutate'), clients[0].request('inspect')]
            assert result['observations'][2]['value'] == 'b'
            assert result['observations'][3]['value'] == 'a'
            result['interruptions'] = [interrupted_run(manager, baseline['id'], report, sig)
                                       for sig in (signal.SIGINT, signal.SIGKILL)]
            assert all(run['path'].is_dir() for run in runs), 'Cleanup removed an active workspace'
            if refresh:
                replacement = manager.prepare(baseline['source'], refresh=True)
                assert replacement['id'] != baseline['id']
                assert all(run['baseline']['id'] == baseline['id'] for run in runs)
                assert manager.resolve()['id'] == replacement['id']
                result['refreshed_baseline'] = replacement['id']
        assert verify_tree(manager.volume / 'baselines' / baseline['id']) == baseline['verification']
        assert not list((manager.volume / 'runs').glob('*.json'))
        result['success'] = True
        return result
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        save(report / 'acceptance.json', result)
