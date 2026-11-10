"""Optional local Btrfs library workspaces; never imported by ordinary fixtures."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VOLUME = ROOT / 'build/library-image-poc/volume'
DEFAULT_IMAGE = 'localhost/tuneconsole-library-tests:local'
IDENTITY = re.compile(r'[0-9a-f]{32}\Z')


def command(*args, check=True, **options):
    result = subprocess.run([str(arg) for arg in args], text=True, capture_output=True, **options)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr.strip()}')
    return result


def save(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def lease(path, *, exclusive=True, blocking=True, wait_seconds=0):
    with path.open('a+b') as stream:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                            | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.025)
        # Close rather than LOCK_UN: launched Podman clients inherit this open
        # description so a killed coordinator cannot free their lifetime lock.
        yield stream


def token(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError('Invalid library generation/workspace identity')
    return value


def process_identity():
    return dict(pid=os.getpid(), start=Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
                boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                namespace=os.readlink('/proc/self/ns/pid'))


def owner_alive(owner):
    if owner['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        return False
    if owner['namespace'] != os.readlink('/proc/self/ns/pid'):
        raise RuntimeError('Cleanup must run in the workspace owner PID namespace')
    try:
        fields = Path(f'/proc/{owner["pid"]}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[0] != 'Z' and fields[19] == owner['start']
    except FileNotFoundError:
        return False


class Manager:
    def __init__(self, volume=DEFAULT_VOLUME, *, image=DEFAULT_IMAGE):
        self.volume = Path(volume).resolve()
        self.image = image

    def initialize(self):
        if command('stat', '-f', '-c', '%T', self.volume).stdout.strip() != 'btrfs':
            raise RuntimeError('Mount the local library image first; no copy fallback is provided')
        for name in ('baselines', 'metadata', 'sources', 'locks', 'workspaces', 'runs'):
            (self.volume / name).mkdir(exist_ok=True)
        return self

    def lock(self):
        return lease(self.volume / '.manager.lock')

    def metadata(self, identity):
        result = json.loads((self.volume / 'metadata' / (token(identity) + '.json')).read_text())
        if result['id'] != identity:
            raise ValueError('Baseline metadata identity mismatch')
        return result

    def resolve(self, *, baseline=None, source=None, schema=None):
        if baseline:
            result = self.metadata(baseline)
        elif schema is not None:
            candidates = [self.metadata(path.stem) for path in (self.volume / 'metadata').glob('*.json')]
            candidates = [item for item in candidates if item['schema'] == schema
                          and (source is None or item['source'] == str(Path(source).resolve()))]
            if not candidates:
                raise ValueError('No prepared generation matches the requested schema')
            result = max(candidates, key=lambda item: item['created_at'])
        else:
            source = Path(source or Path.home() / '.local/share/yt-playlist/state.db').resolve()
            key = hashlib.sha256(str(source).encode()).hexdigest()
            pointer = self.volume / 'sources' / (key + '.json')
            if not pointer.exists():
                raise ValueError('No prepared baseline for this source; run prepare explicitly')
            result = self.metadata(json.loads(pointer.read_text())['baseline'])
        if schema is not None and result['schema'] != schema:
            raise ValueError('Prepared generation has a different schema')
        path = self.volume / 'baselines' / token(result['id'])
        if not path.is_dir() or command('btrfs', 'property', 'get', '-ts', path, 'ro').stdout.strip() != 'ro=true':
            raise RuntimeError('Prepared baseline is missing or is not frozen')
        return result

    def container_args(self, library, report, name, *, readonly=False, donor=None):
        return [
            'podman', 'run', '--rm', '--name', name, '--pull=never', '--userns=keep-id',
            '--network=none', '--read-only', '--cap-drop=all', '--security-opt=label=disable',
            '--memory=2g', '--pids-limit=256', '--stop-timeout=0',
            '--tmpfs', '/tmp:rw,size=256m,mode=1777,nodev,nosuid,notmpcopyup',
            '--tmpfs', '/worktree/build:rw,size=256m,mode=1777,nodev,nosuid,notmpcopyup',
            '--volume', str(ROOT) + ':/worktree:ro',
            '--volume', str(library) + ':/library:' + ('ro' if readonly else 'rw'),
            '--volume', str(report) + ':/reports:rw',
            *(['--volume', str(donor.parent) + ':' + str(donor.parent) + ':ro'] if donor else []),
            '--workdir', '/worktree', '--env', 'PYTHONPATH=/worktree:/worktree/src',
            '--env', 'TUNECONSOLE_IMAGE_RUN=1', '--env', 'TUNECONSOLE_TEST_LIBRARY=/library/state.db',
            '--env', 'TUNECONSOLE_BENCH_DB=/library/state.db',
            '--env', 'HOME=/tmp/home', '--env', 'YT_PLAYLIST_HOME=/tmp/app-home',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--entrypoint', 'python3', self.image]

    def stop(self, name):
        if not re.fullmatch(r'tuneconsole-library-(?:prepare|run)-[0-9a-f]{32}', name):
            raise ValueError('Refusing an unmanaged container name')
        command('podman', 'rm', '--force', '--ignore', '--time', '0', name, timeout=20)

    def destroy(self, path):
        command('btrfs', 'property', 'set', '-ts', path, 'ro', 'false')
        command('btrfs', 'subvolume', 'delete', path)

    def prepare(self, source, *, refresh=False):
        from .capture import verify_tree

        self.initialize()
        source = Path(source).resolve(strict=True)
        key = hashlib.sha256(str(source).encode()).hexdigest()
        pointer = self.volume / 'sources' / (key + '.json')
        with lease(self.volume / 'locks' / ('prepare-' + key + '.lock')):
            self.collect(delete=True)
            if pointer.exists() and not refresh:
                return self.resolve(source=source)
            # Leave space for the candidate instead of temporarily retaining
            # three full captures. Current source generations and active pins
            # remain protected even when older unused generations are pruned.
            self.collect(keep=1, delete=True)
            identity = uuid.uuid4().hex
            target = self.volume / 'baselines' / identity
            report_dir = ROOT / 'build/test-reports/library-images' / identity
            report_dir.mkdir(parents=True)
            name = 'tuneconsole-library-prepare-' + identity
            receipt = dict(id=identity, source=str(source), created_at=time.time(), state='preparing',
                           container=name, kind='prepare', owner=process_identity())
            with lease(self.volume / 'locks' / (identity + '.lock')) as held:
                try:
                    with self.lock():
                        save(self.volume / 'runs' / (identity + '.json'), receipt)
                        command('btrfs', 'subvolume', 'create', target)
                    started = time.monotonic()
                    before = source.stat()
                    print('Capturing one consistent SQL/file baseline', flush=True)
                    command(*self.container_args(target, report_dir, name, donor=source), '-m',
                            'tests.library_image.capture', 'export', str(source), pass_fds=(held.fileno(),))
                    receipt['export_seconds'] = time.monotonic() - started
                    receipt.update(json.loads((target / '.capture.json').read_text()))
                    (target / '.capture.json').unlink()
                    print('Restoring and checking stable /library coordinates', flush=True)
                    command(*self.container_args(target, report_dir, name), '-m',
                            'tests.library_image.capture', 'finish', pass_fds=(held.fileno(),))
                    receipt['restore_seconds'] = time.monotonic() - started - receipt['export_seconds']
                    after = source.stat()
                    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        raise RuntimeError('Source database was replaced during capture; retry preparation')
                    save(target / '.baseline.json', dict(id=identity, schema=receipt['schema'],
                                                        created_at=receipt['created_at']))
                    command('btrfs', 'filesystem', 'sync', target)
                    receipt['verification'] = verify_tree(target)
                    receipt.update(seconds=time.monotonic() - started, state='ready',
                                   runtime_image=command('podman', 'image', 'inspect', '--format', '{{.Id}}', self.image).stdout.strip(),
                                   source_stat=dict(device=before.st_dev, inode=before.st_ino))
                    with self.lock():
                        command('btrfs', 'property', 'set', '-ts', target, 'ro', 'true')
                        save(self.volume / 'metadata' / (identity + '.json'), receipt)
                        save(pointer, {'baseline': identity})
                    save(report_dir / 'preparation.json', receipt)
                    self.collect(delete=True)
                    return receipt
                except BaseException as error:
                    save(report_dir / 'preparation-error.json', dict(receipt, error=repr(error)))
                    raise
                finally:
                    self.stop(name)
                    with self.lock():
                        if not (self.volume / 'metadata' / (identity + '.json')).exists() and target.exists():
                            self.destroy(target)
                        (self.volume / 'runs' / (identity + '.json')).unlink(missing_ok=True)

    @contextmanager
    def pinned(self, **selection):
        with self.lock():
            baseline = self.resolve(**selection)
            pin = lease(self.volume / 'locks' / ('baseline-' + baseline['id'] + '.lock'), exclusive=False)
            stream = pin.__enter__()
        try:
            yield baseline, stream
        finally:
            pin.__exit__(None, None, None)

    @contextmanager
    def workspace(self, *, report, readonly=False, **selection):
        self.initialize()
        report = Path(report).resolve()
        roots = (ROOT / 'build/test-reports', Path('/tmp'))
        if not any(report != root and report.is_relative_to(root) for root in roots) or report.is_relative_to(self.volume):
            raise ValueError('Reports must be in a private directory under build/test-reports or /tmp, outside the image')
        report.mkdir(parents=True, exist_ok=True)
        identity = uuid.uuid4().hex
        name = 'tuneconsole-library-run-' + identity
        path = self.volume / 'workspaces' / identity
        with lease(self.volume / 'locks' / (identity + '.lock')) as held, self.pinned(**selection) as (baseline, pinned):
            try:
                with self.lock():
                    started = time.monotonic()
                    base = self.volume / 'baselines' / baseline['id']
                    receipt = dict(id=identity, baseline=baseline['id'], created_at=time.time(),
                                   container=name, kind='readonly' if readonly else 'workspace', owner=process_identity())
                    save(self.volume / 'runs' / (identity + '.json'), receipt)
                    if not readonly:
                        command('btrfs', 'subvolume', 'snapshot', base, path)
                    receipt['acquisition_seconds'] = time.monotonic() - started
                    save(report / 'workspace.json', receipt)
                yield dict(path=base if readonly else path, name=name, baseline=baseline,
                           fds=(held.fileno(), pinned.fileno()), receipt=receipt, report=report)
            finally:
                # Release no lifetime pin until both Podman and its processes stop.
                self.stop(name)
                with self.lock():
                    if path.exists():
                        self.destroy(path)
                    (self.volume / 'runs' / (identity + '.json')).unlink(missing_ok=True)

    def execute(self, arguments, *, report, readonly=False, **selection):
        with self.workspace(report=report, readonly=readonly, **selection) as run:
            started = time.monotonic()
            with (run['report'] / 'output.log').open('wb') as output:
                with subprocess.Popen(self.container_args(run['path'], run['report'], run['name'], readonly=readonly)
                                      + list(arguments), pass_fds=run['fds'], stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT) as child:
                    try:
                        for line in child.stdout:
                            output.write(line)
                            output.flush()
                            sys.stdout.buffer.write(line)
                            sys.stdout.buffer.flush()
                        code = child.wait()
                    except BaseException:
                        # Popen.__exit__ waits: stop the container before entering
                        # that wait when an interrupt arrives during a long check.
                        self.stop(run['name'])
                        raise
            save(run['report'] / 'result.json', dict(returncode=code, seconds=time.monotonic() - started,
                                                     **run['receipt']))
            return code

    def collect(self, *, keep=2, delete=False, workspace=None):
        """Preview or remove abandoned work and old unpinned baseline generations."""
        if keep < 1:
            raise ValueError('Keep at least one baseline generation')
        if workspace is not None:
            token(workspace)
        self.initialize()
        removed, protected = [], set()
        with self.lock():
            receipts = sorted((self.volume / 'runs').glob('*.json')) if workspace is None else [
                self.volume / 'runs' / (workspace + '.json')]
            for path in receipts:
                receipt = json.loads(path.read_text())
                identity = token(receipt['id'])
                if path.stem != identity:
                    raise ValueError('Run receipt identity mismatch')
                expected = 'tuneconsole-library-' + ('prepare-' if receipt['kind'] == 'prepare' else 'run-') + identity
                if receipt['container'] != expected or receipt['kind'] not in ('prepare', 'workspace', 'readonly'):
                    raise ValueError('Run receipt container/kind mismatch')
                if workspace is not None and receipt['kind'] != 'workspace':
                    raise ValueError('Target is not a disposable managed workspace')
                if owner_alive(receipt['owner']):
                    protected.add(receipt.get('baseline', identity))
                    continue
                state = command('podman', 'container', 'exists', receipt['container'], check=False)
                if state.returncode not in (0, 1):
                    raise RuntimeError('Cannot establish container lifetime: ' + state.stderr)
                if state.returncode == 0:
                    removed.append('container:' + receipt['container'])
                    if delete:
                        self.stop(receipt['container'])
                    else:
                        protected.add(receipt.get('baseline', identity))
                        continue
                try:
                    with lease(self.volume / 'locks' / (identity + '.lock'), blocking=False,
                               wait_seconds=1 if delete and state.returncode == 0 else 0):
                        target = self.volume / ('baselines' if receipt['kind'] == 'prepare' else 'workspaces') / identity
                        if receipt['kind'] != 'readonly' and target.exists() and not (
                                receipt['kind'] == 'prepare' and (self.volume / 'metadata' / (identity + '.json')).exists()):
                            removed.append(str(target))
                            if delete:
                                self.destroy(target)
                        if delete:
                            path.unlink()
                except BlockingIOError:
                    protected.add(receipt.get('baseline', identity))
            if workspace is not None:
                return removed
            generations = [self.metadata(path.stem) for path in (self.volume / 'metadata').glob('*.json')]
            latest = {json.loads(path.read_text())['baseline'] for path in (self.volume / 'sources').glob('*.json')}
            retained = sorted(generations, key=lambda item: item['created_at'], reverse=True)
            for item in retained[max(0, keep):]:
                identity = token(item['id'])
                if identity in protected or identity in latest:
                    continue
                try:
                    with lease(self.volume / 'locks' / ('baseline-' + identity + '.lock'), blocking=False):
                        target = self.volume / 'baselines' / identity
                        removed.append(str(target))
                        if delete:
                            self.destroy(target)
                            (self.volume / 'metadata' / (identity + '.json')).unlink()
                except BlockingIOError:
                    continue
        return removed
