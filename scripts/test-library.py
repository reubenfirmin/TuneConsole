#!/usr/bin/env python3
"""Prepare once and run local actual-library checks in private CoW snapshots."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from library_image import DEFAULT_IMAGE, DEFAULT_VOLUME, Manager, ROOT, command


def runtime():
    directory = ROOT / 'build/library-image-runtime'
    directory.mkdir(parents=True, exist_ok=True)
    command('uv', 'export', '--cache-dir', ROOT / 'build/uv-cache', '--frozen', '--group', 'dev',
            '--no-emit-project', '--format', 'requirements-txt',
            '--output-file', directory / 'requirements.txt')
    fingerprint = hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest()
    subprocess.run(['podman', 'build', '--pull=never', '--build-arg', 'LOCK_SHA256=' + fingerprint,
                    '-f', str(ROOT / 'scripts/library-tests.Containerfile'),
                    '-t', DEFAULT_IMAGE, str(directory)], check=True)


def check_runtime(manager):
    actual = command('podman', 'image', 'inspect', '--format',
                     '{{index .Labels "org.tuneconsole.library-tests.lock-sha256"}}', manager.image).stdout.strip()
    if actual != hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest():
        raise ValueError('Local runtime does not match uv.lock; run build-runtime explicitly')


def pytest_checks(manager, arguments, report, selection):
    collection = report / 'collection'
    extra = ['-p', 'library_image_pytest', '-n', '0', '-o', 'cache_dir=/tmp/pytest-cache',
             '--fixture-root=/tmp/pytest-fixtures', '--junitxml=/reports/pytest.xml']
    code = manager.execute(['-m', 'pytest', *arguments, *extra, '--collect-only',
                            '--library-collection-report=/reports/nodes.json'], report=collection,
                           readonly=True, **selection)
    if code:
        return code
    nodes = json.loads((collection / 'nodes.json').read_text())
    if not nodes:
        raise ValueError('No tests selected')
    results = []
    for index, node in enumerate(nodes):
        print(f'[{index + 1}/{len(nodes)}] {node}', flush=True)
        destination = report / f'test-{index + 1:04}'
        code = manager.execute(['-m', 'pytest', *arguments, *extra, '--library-node-id=' + node],
                               report=destination, **selection)
        results.append(dict(nodeid=node, returncode=code, report=str(destination)))
        (report / 'tests.json').write_text(json.dumps(results, indent=2) + '\n')
        if code:
            return code
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--volume', type=Path, default=DEFAULT_VOLUME)
    parser.add_argument('--image', default=DEFAULT_IMAGE)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('build-runtime', help='Build the local dependency image from uv.lock')
    prepare = actions.add_parser('prepare', help='Reuse the prepared baseline, or explicitly refresh it')
    prepare.add_argument('--source', type=Path, default=Path.home() / '.local/share/yt-playlist/state.db')
    prepare.add_argument('--refresh', action='store_true')
    run = actions.add_parser('run', help='Run Python, or isolated focused pytest tests')
    run.add_argument('--source', type=Path)
    run.add_argument('--baseline')
    run.add_argument('--schema', type=int)
    run.add_argument('--report', type=Path)
    run.add_argument('--readonly', action='store_true')
    run.add_argument('arguments', nargs=argparse.REMAINDER)
    gc = actions.add_parser('gc', help='Preview abandoned work and old unpinned generations')
    gc.add_argument('--delete', action='store_true')
    gc.add_argument('--keep', type=int, default=2)
    actions.add_parser('list', help='List prepared generations without scanning their contents')
    args = parser.parse_args(argv)
    if args.action == 'build-runtime':
        runtime()
        return 0
    manager = Manager(args.volume, image=args.image).initialize()
    if args.action == 'prepare':
        check_runtime(manager)
        print(json.dumps(manager.prepare(args.source, refresh=args.refresh), indent=2))
    elif args.action == 'gc':
        if args.keep < 1:
            parser.error('--keep must be at least one')
        print(json.dumps(dict(delete=args.delete, paths=manager.collect(keep=args.keep, delete=args.delete)), indent=2))
    elif args.action == 'list':
        print(json.dumps([json.loads(path.read_text()) for path in (manager.volume / 'metadata').glob('*.json')], indent=2))
    else:
        arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
        if not arguments:
            parser.error('Supply pytest <focused paths> or Python arguments after --')
        if arguments[0] == 'pytest':
            if args.readonly:
                parser.error('Use a read-only Python inspection, or give each pytest case its writable workspace')
            paths = [item.split('::', 1)[0] for item in arguments[1:] if not item.startswith('-')]
            if not any(Path(item).is_file() and item.endswith('.py') for item in paths):
                parser.error('Select specific test files or node ids; directories/global suites are not supported')
            if any(Path(item).is_dir() for item in paths):
                parser.error('Select test files instead of directories')
        check_runtime(manager)
        report = (args.report or ROOT / 'build/test-reports/library-images' / ('run-' + uuid.uuid4().hex)).resolve()
        selection = dict(baseline=args.baseline, source=args.source, schema=args.schema)
        # Pin the selected generation across collection and all per-test runs,
        # including refresh by another agent between two selected test cases.
        with manager.pinned(**selection) as (metadata, _held):
            selection = dict(baseline=metadata['id'])
            if arguments[0] == 'pytest':
                return pytest_checks(manager, arguments[1:], report, selection)
            if arguments[0] in ('python', 'python3'):
                arguments = arguments[1:]
            return manager.execute(arguments, report=report, readonly=args.readonly, **selection)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
