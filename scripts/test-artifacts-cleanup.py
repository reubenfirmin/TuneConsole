#!/usr/bin/env python3
"""Remove explicitly named, inactive TuneConsole test workspaces.

Preview:  ./scripts/test-artifacts-cleanup.py /tmp/tuneconsole-example-workspace
Delete:   ./scripts/test-artifacts-cleanup.py --delete /tmp/tuneconsole-example-workspace
Register a new disposable workspace with its actual test/experiment PID:
          ./scripts/test-artifacts-cleanup.py --register --owner-pid 12345 PATH

Registration lets cleanup distinguish a stopped owner from unrelated test jobs.
Unregistered legacy workspaces require all test/experiment jobs to be idle.
Private review source snapshots directly under build/ can be removed once no
process references them; their fixtures and process temporary files are inside.
Reports should live beside a disposable workspace, never inside it. This command
does not kill processes, acquire the suite lock, or delete shared lock files.
"""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

MARKER = '.tuneconsole-test-artifact.json'
REPOSITORY = Path(__file__).resolve().parents[1]
MEMORY_ROOTS = frozenset('tuneconsole-' + name for name in (
    'served', 'groups', 'metadata', 'metadata-runtime', 'archives', 'policies',
    'recommendations', 'collections'))
UUID = re.compile(r'[0-9a-f]{32}\Z')
BUILD_PREFIXES = ('review748-', 'review-browser-fixes4-')


def private_build_workspace(path):
    return path.parent == REPOSITORY / 'build' and path.name.startswith(BUILD_PREFIXES)


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


def processes():
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
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
            result.append(Process(int(entry.name), '', (), (), unreadable=True))
    return result


def validate_path(value, *, registering=False):
    path = Path(os.path.abspath(value))
    if path == Path('/tmp') or path == REPOSITORY / '.pytest_cache':
        raise ValueError('shared parent directory')
    if not (path.is_relative_to('/tmp') or path.is_relative_to(REPOSITORY / '.pytest_cache')
            or private_build_workspace(path)):
        raise ValueError('outside supported temporary workspace locations')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('symlink in target path')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('target must be a directory owned by the current user')
    if path.parent == Path('/tmp') and path.name in MEMORY_ROOTS:
        raise ValueError('shared file-domain parent')
    if path.parent == Path('/tmp') and path.name.startswith('pytest-of-'):
        raise ValueError('shared pytest parent')
    if path.name.startswith('tuneconsole-tests-'):
        raise ValueError('protected suite lock')
    if path.name.startswith('tuneconsole-review') and 'workspace' not in path.name:
        raise ValueError('review evidence directory; select its disposable child')
    registered = path / MARKER
    memory = path.parent.name in MEMORY_ROOTS and bool(UUID.fullmatch(path.name))
    workspace = (path.name.startswith(('runtime-', 'negative-runtime-', 'fixtures'))
                 and any(p.name.startswith('tuneconsole-review') for p in path.parents))
    workspace |= path.parent == Path('/tmp') and path.name.startswith('tuneconsole-') and 'workspace' in path.name
    workspace |= private_build_workspace(path)
    pytest_run = path.parent.name.startswith('pytest-of-') and bool(re.fullmatch(r'pytest-\d+', path.name))
    new_workspace = registering and (path.is_relative_to(REPOSITORY / '.pytest_cache')
        or any(p.name.startswith('tuneconsole-') for p in (path, *path.parents) if p.parent == Path('/tmp')))
    if not (registered.exists() or memory or workspace or pytest_run or new_workspace):
        raise ValueError('not a registered workspace or recognized disposable test directory')
    return path, info


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


def blocker(path, running):
    saved = receipt(path)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    needle = str(path)
    for process in running:
        if process.pid == os.getpid():
            continue
        if process.unreadable:
            return f'cannot inspect process {process.pid}'
        if (saved and saved['boot'] == boot and process.pid == saved['pid']
                and process.start == saved['start']):
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
        if saved is None and test_job(process) and not private_build_workspace(path):
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
    parser.add_argument('paths', nargs='+', help='explicit disposable directories; wildcards are not expanded here')
    args = parser.parse_args(argv)
    if args.register != (args.owner_pid is not None):
        parser.error('--register and --owner-pid must be used together')
    if not shutil.rmtree.avoids_symlink_attacks:
        parser.error('safe directory removal requires fd-based shutil.rmtree on this platform')
    failed = False
    for value in args.paths:
        try:
            path, original = validate_path(value, registering=args.register)
            if args.register:
                register(path, args.owner_pid, processes())
                print(f'REGISTERED {path} owner={args.owner_pid}')
                continue
            reason = blocker(path, processes())
            if reason:
                print(f'SKIP {path}: {reason}')
                failed = True
                continue
            if args.delete:
                # Recheck identity and processes immediately before the fd-safe
                # removal. Explicit targets are never discovered recursively.
                current = path.stat()
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise ValueError('directory changed during inspection')
                reason = blocker(path, processes())
                if reason:
                    raise ValueError(reason)
                shutil.rmtree(path)
                print(f'DELETED {path}')
            else:
                print(f'WOULD DELETE {path}')
        except (OSError, ValueError, KeyError) as error:
            print(f'SKIP {value}: {error}')
            failed = True
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
