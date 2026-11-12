#!/usr/bin/env python3
"""Discover and remove inactive TuneConsole test workspaces.

Preview:  ./scripts/test-artifacts-cleanup.py
Delete:   ./scripts/test-artifacts-cleanup.py --delete
Optional PATH arguments restrict cleanup to those disposable directories.
Use --verbose to list individual paths instead of discovery totals by parent.
Register a new disposable workspace with its actual test/experiment PID:
          ./scripts/test-artifacts-cleanup.py --register --owner-pid 12345 PATH

Registration lets cleanup distinguish a stopped owner from unrelated test jobs.
Unregistered legacy workspaces require all test/experiment jobs to be idle.
Private review source snapshots directly under build/ can be removed once no
process references them; their fixtures and process temporary files are inside.
Default pytest bases under build/pytest include their owning PID and are eligible
after that owner stops and no process references the directory.
For workspaces with a recorded owner, unrelated unreadable desktop processes do
not veto cleanup. Live owners, observed references and unreadable test jobs still
block. Identified systemd user-session processes and the Sway compositor do not
veto legacy cleanup. Unknown unreadable processes and live test/experiment jobs
still block it.
Discovery visits known temporary/fixture locations, not arbitrary directories.
Preview uses one process snapshot. Deletion refreshes checks before each
individual workspace removal, including between removals in the same batch.
Run from the host PID namespace so live workspace owners are visible.
Reports should live beside a disposable workspace, never inside it. Managed
library workspaces use their manager's leases and receipts; only an orphan's
own container is stopped before deleting its snapshot. Other cleanup never
kills processes. This command does not acquire or delete suite locks.
"""
import argparse
from collections import Counter
from dataclasses import dataclass
from itertools import batched
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

MARKER = '.tuneconsole-test-artifact.json'
REPOSITORY = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path('/tmp')
MEMORY_ROOTS = frozenset('tuneconsole-' + name for name in (
    'served', 'groups', 'metadata', 'metadata-runtime', 'archives', 'policies', 'evidence',
    'recommendations', 'collections'))
UUID = re.compile(r'[0-9a-f]{32}\Z')
MEMORY_KEY = re.compile(r'(?:[0-9a-f]{32}|[1-9][0-9]{5,19})\Z')
BATCH_SIZE = 128
BUILD_PREFIXES = ('review748-', 'review573-', 'review-browser-fixes4-', '686-focused-', '686-http-',
                  '686-runtime-qualification-')


def private_build_workspace(path):
    return path.parent == REPOSITORY / 'build' and path.name.startswith(BUILD_PREFIXES)


def private_fixture_workspace(path):
    return (path.parent == REPOSITORY / 'build' / 'pytest'
            and re.fullmatch(r'run-(?:sqlite-)?[1-9][0-9]*-[0-9a-f]{32}', path.name) is not None)


def legacy_schema_workspace(path):
    return (path.parent == REPOSITORY / 'build' / 'schema-acceptance'
            and re.fullmatch(r'schema[1-9][0-9]*-[a-z0-9_]{8}', path.name) is not None)


def private_image_workspace(path):
    return (path.parent == REPOSITORY / 'build/library-image-poc/volume'
            and re.fullmatch(r'proof-[a-z0-9_]{8}', path.name) is not None)


def managed_image_workspace(path):
    return (path.parent == REPOSITORY / 'build/library-image-poc/volume/workspaces'
            and UUID.fullmatch(path.name) is not None)


def managed_cleanup(path, *, delete):
    sys.path.insert(0, str(REPOSITORY))
    from tests.library_image.manager import Manager
    return Manager(path.parent.parent).collect(workspace=path.name, delete=delete)


def remove_workspace(path):
    if private_image_workspace(path):
        # A frozen PoC baseline cannot be recursively unlinked. All users have
        # been checked by blocker() before thawing these disposable subvolumes.
        children = list(path.iterdir())
        for child in children:
            if child.is_dir() and (child.is_symlink() or child.name not in ('baseline', 'a', 'b')):
                raise ValueError('unexpected directory in image PoC workspace')
        for child in children:
            if child.is_dir():
                for operation in (['property', 'set', '-ts', str(child), 'ro', 'false'],
                                  ['subvolume', 'delete', str(child)]):
                    subprocess.run(['btrfs', *operation], check=True, capture_output=True, text=True)
    shutil.rmtree(path)


@dataclass
class Process:
    pid: int
    start: str
    command: tuple[str, ...]
    references: tuple[str, ...]
    unreadable: bool = False


def process_start(path):
    # comm may itself contain spaces and parentheses. Field 22 is starttime.
    return (path / 'stat').read_text().rsplit(')', 1)[1].split()[19]


def processes(root=Path('/proc')):
    if root == Path('/proc') and (root / '1/comm').read_text().strip() not in ('systemd', 'init'):
        raise ValueError('host process visibility required; run cleanup from the host PID namespace')
    result = []
    for entry in root.iterdir():
        if not entry.name.isdecimal():
            continue
        start, command, references = '', (), []
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            start = process_start(entry)
            command = tuple(part.decode(errors='replace') for part in
                            (entry / 'cmdline').read_bytes().split(b'\0') if part)
            references = [os.readlink(entry / 'cwd')]
            for descriptor in (entry / 'fd').iterdir():
                try:
                    references.append(os.readlink(descriptor))
                except FileNotFoundError:
                    pass
            for line in (entry / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) == 6:
                    references.append(fields[5])
            result.append(Process(int(entry.name), start, command, tuple(references)))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # A denied read must not erase an owner identity or a reference we
            # already observed before that read.
            result.append(Process(int(entry.name), start, command, tuple(references), unreadable=True))
    return result


def validate_path(value, *, registering=False):
    path = Path(os.path.abspath(value))
    if path == TEMP_ROOT or path == REPOSITORY / '.pytest_cache':
        raise ValueError('shared parent directory')
    if not (path.is_relative_to(TEMP_ROOT) or path.is_relative_to(REPOSITORY / '.pytest_cache')
            or private_build_workspace(path) or private_fixture_workspace(path) or legacy_schema_workspace(path)
            or private_image_workspace(path)
            or managed_image_workspace(path)):
        raise ValueError('outside supported temporary workspace locations')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('symlink in target path')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('target must be a directory owned by the current user')
    if path.parent == TEMP_ROOT and path.name in MEMORY_ROOTS:
        raise ValueError('shared file-domain parent')
    if path.parent == TEMP_ROOT and path.name.startswith('pytest-of-'):
        raise ValueError('shared pytest parent')
    if path.name.startswith('tuneconsole-tests-'):
        raise ValueError('protected suite lock')
    if path.name.startswith('tuneconsole-review') and 'workspace' not in path.name:
        raise ValueError('review evidence directory; select its disposable child')
    registered = path / MARKER
    memory = (path.parent.parent == TEMP_ROOT and path.parent.name in MEMORY_ROOTS
              and bool(MEMORY_KEY.fullmatch(path.name)))
    workspace = (path.name.startswith(('runtime-', 'negative-runtime-', 'fixtures'))
                 and any(p.name.startswith('tuneconsole-review') for p in path.parents))
    workspace |= path.parent == TEMP_ROOT and path.name.startswith('tuneconsole-') and 'workspace' in path.name
    workspace |= (private_build_workspace(path) or private_fixture_workspace(path) or private_image_workspace(path)
                  or managed_image_workspace(path) or legacy_schema_workspace(path))
    pytest_run = path.parent.name.startswith('pytest-of-') and bool(re.fullmatch(r'pytest-\d+', path.name))
    new_workspace = registering and (path.is_relative_to(REPOSITORY / '.pytest_cache')
        or any(p.name.startswith('tuneconsole-') for p in (path, *path.parents) if p.parent == TEMP_ROOT))
    if not (registered.exists() or memory or workspace or pytest_run or new_workspace):
        raise ValueError('not a registered workspace or recognized disposable test directory')
    return path, info


def discover():
    """Select recognized owned directories without descending into their data."""
    def children(root):
        if root.is_symlink() or not root.is_dir():
            return ()
        if root != TEMP_ROOT and root.stat().st_uid != os.getuid():
            return ()
        return tuple(p for p in root.iterdir() if not p.is_symlink() and p.is_dir())

    def candidates():
        for path in children(TEMP_ROOT):
            yield path
            if path.name in MEMORY_ROOTS or path.name.startswith('pytest-of-'):
                yield from children(path)
            elif path.name.startswith('tuneconsole-review'):
                # Evidence stays in its parent; only recognized disposable children qualify.
                for child in children(path):
                    yield child
                    yield from children(child)
        for root in (REPOSITORY / 'build', REPOSITORY / 'build/pytest',
                     REPOSITORY / 'build/schema-acceptance',
                     REPOSITORY / 'build/library-image-poc/volume',
                     REPOSITORY / 'build/library-image-poc/volume/workspaces'):
            yield from children(root)
        cache = REPOSITORY / '.pytest_cache'
        if cache.is_dir() and not cache.is_symlink():
            for root, dirs, files in os.walk(cache, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                if MARKER in files:
                    yield Path(root)
                    dirs.clear()

    selected = set()
    for value in candidates():
        try:
            path, _ = validate_path(value)
        except (OSError, ValueError):
            continue
        selected.add(path)
    result = []
    for path in sorted(selected, key=lambda p: (len(p.parts), str(p))):
        if not any(parent in selected for parent in path.parents):
            result.append(path)
    return result


def receipt(path):
    marker = path / MARKER
    if not marker.exists():
        return None
    if marker.is_symlink() or marker.stat().st_uid != os.getuid():
        raise ValueError('invalid workspace marker')
    value = json.loads(marker.read_text())
    if not isinstance(value, dict):
        raise ValueError('workspace marker must be an object')
    info = path.stat()
    if (value.get('version') != 1 or value.get('path') != str(path)
            or value.get('uid') != os.getuid() or value.get('device') != info.st_dev
            or value.get('inode') != info.st_ino or type(value.get('pid')) is not int
            or value['pid'] <= 0 or not isinstance(value.get('start'), str)):
        raise ValueError('workspace marker does not match this directory')
    return value


def test_job(process):
    args = process.command
    return any(Path(arg).name in ('pytest', 'py.test', 'pytest.py')
               or arg.endswith(('test_review.py', 'test_negatives_review.py'))
               or (' ' not in arg and (arg.startswith('experiments/') or '/experiments/' in arg))
               for arg in args)


def session_process(process):
    return process.command in (('/usr/lib/systemd/systemd', '--user'), ('(sd-pam)',), ('/usr/bin/sway',))


class ProcessSnapshot:
    """Index references once, rather than scan every descriptor for every path."""
    def __init__(self, running):
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.processes = {process.pid: process for process in running if process.pid != os.getpid()}
        self.general = {pid for pid, process in self.processes.items()
                        if test_job(process) or process.unreadable and not session_process(process)}
        self.references = {}
        self.starts = {}
        for pid, process in self.processes.items():
            if process.start:
                self.starts.setdefault(process.start, set()).add(pid)
            for reference in process.references:
                reference = reference.removesuffix(' (deleted)')
                if not reference.startswith((str(TEMP_ROOT) + '/', str(REPOSITORY) + '/')):
                    continue
                path = Path(reference)
                for parent in (path, *path.parents):
                    self.references.setdefault(parent, set()).add(pid)

    def relevant(self, path, saved):
        pids = self.general | self.references.get(path, set())
        if saved:
            pids.add(saved['pid'])
            pids.update(self.starts.get(saved['start'], ()))
        if private_fixture_workspace(path):
            pids.add(int(path.name.rsplit('-', 2)[1]))
        return (self.processes[pid] for pid in sorted(pids) if pid in self.processes)


def blocker(path, running):
    saved = receipt(path)
    owned = saved is not None or private_fixture_workspace(path)
    if isinstance(running, ProcessSnapshot):
        boot = running.boot
        running = running.relevant(path, saved)
    else:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    needle = str(path)
    for process in running:
        if process.pid == os.getpid():
            continue
        if private_fixture_workspace(path) and process.pid == int(path.name.rsplit('-', 2)[1]):
            return f'owner process {process.pid} is still running'
        if (saved and saved['boot'] == boot
                and ((process.pid == saved['pid'] and not process.start)
                     or process.start and process.start == saved['start'])):
            # A receipt may have recorded the owner's PID inside a test sandbox.
            # Same-boot start ticks still identify possible live owners on the host.
            return f'owner process {process.pid} is still running'
        if any(ref.removesuffix(' (deleted)') == needle or ref.startswith(needle + '/')
               for ref in process.references):
            return f'process {process.pid} has an open file, mapping or cwd in this workspace'
        if any(arg == needle or arg.startswith(needle + '/') or arg.endswith('=' + needle)
               or ('=' + needle + '/') in arg for arg in process.command):
            # The invoking shell may contain this cleanup command. It cannot be
            # a workspace owner, but a pytest/experiment command must still block.
            if test_job(process):
                return f'test process {process.pid} names this workspace'
        if process.unreadable:
            if (not owned and not session_process(process)) or test_job(process):
                return f'cannot inspect process {process.pid}'
            continue
        if (saved is None and test_job(process)
                and not (private_build_workspace(path) or private_fixture_workspace(path))):
            return f'unregistered workspace while test/experiment process {process.pid} is running'
    return None


def register(path, pid, running):
    info = path.stat()
    owner = next((p for p in running if p.pid == pid and not p.unreadable), None)
    if owner is None:
        raise ValueError('owner PID must be a running, inspectable process belonging to this user')
    value = {'version': 1, 'path': str(path), 'uid': os.getuid(), 'device': info.st_dev,
             'inode': info.st_ino, 'pid': pid, 'start': owner.start,
             'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    descriptor = os.open(path / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--delete', action='store_true', help='delete eligible targets; default is a dry run')
    actions.add_argument('--register', action='store_true', help='mark a disposable workspace with its live owner')
    parser.add_argument('--owner-pid', type=int, help='actual test/experiment owner PID, required for registration')
    parser.add_argument('--verbose', action='store_true', help='list each discovered path')
    parser.add_argument('paths', nargs='*', help='optional disposable directories; default: discover known artifacts')
    args = parser.parse_args(argv)
    if args.register != (args.owner_pid is not None):
        parser.error('--register and --owner-pid must be used together')
    if args.register and not args.paths:
        parser.error('--register requires explicit workspace paths')
    if not shutil.rmtree.avoids_symlink_attacks:
        parser.error('safe directory removal requires fd-based shutil.rmtree on this platform')
    totals = Counter()

    def report(action, path, reason=''):
        if args.paths or args.verbose:
            print(f'{action} {path}' + (f': {reason}' if reason else ''), flush=True)
        else:
            totals[action, path.parent, reason] += 1

    try:
        paths = args.paths or discover()
    except OSError as error:
        print(f'SKIP discovery: {error}', flush=True)
        return 1
    failed = False
    running = None
    for batch in batched(paths, BATCH_SIZE):
        try:
            if running is None or args.delete or args.register:
                running = ProcessSnapshot(processes())
        except (OSError, ValueError) as error:
            print(f'SKIP cleanup: {error}', flush=True)
            return 1
        pending = []
        for value in batch:
            try:
                path, original = validate_path(value, registering=args.register)
                if managed_image_workspace(path):
                    if args.register:
                        raise ValueError('Library manager owns this workspace receipt')
                    actions = managed_cleanup(path, delete=args.delete)
                    if not actions or (args.delete and path.exists()):
                        raise ValueError('Managed workspace is active or its lifetime lease is still held')
                    report('DELETED' if args.delete else 'WOULD RECOVER', path)
                    continue
                if args.register:
                    register(path, args.owner_pid, running.processes.values())
                    report('REGISTERED', path, f'owner={args.owner_pid}')
                    continue
                reason = blocker(path, running)
                if reason:
                    raise ValueError(reason)
                if args.delete:
                    pending.append((path, original))
                else:
                    report('WOULD DELETE', path)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
                report('SKIP', Path(value), str(error))
                failed = True
        if not pending:
            continue
        for path, original in pending:
            try:
                running = ProcessSnapshot(processes())
                # Revalidate paths as well as identity after the fresh process scan.
                _, current = validate_path(path)
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise ValueError('directory changed during inspection')
                reason = blocker(path, running)
                if reason:
                    raise ValueError(reason)
                remove_workspace(path)
                report('DELETED', path)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
                report('SKIP', path, str(error))
                failed = True
    for (action, parent, reason), count in sorted(totals.items()):
        print(f'{action} {count} workspace(s) under {parent}' + (f': {reason}' if reason else ''), flush=True)
    if not paths:
        print('No recognized test artifacts found.')
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
