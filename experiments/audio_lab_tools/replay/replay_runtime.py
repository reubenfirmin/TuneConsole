"""Freeze application source and assets for an isolated library replay.

This copies exact working-tree bytes, including uncommitted implementation work.
It does not restore files, create a checkout, or change the running application.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'experiments/audio_lab/local/external-20261028/checkpoint-1/library-replay'
ENVIRONMENT = 'TUNECONSOLE_REPLAY_RUNTIME'
BENCHMARKS = ('experiments/investigations/bench_musical_character.py', 'tests/bench_svd_background.py',
              'experiments/investigations/bench_svd_background.py',
              'tests/musical_evaluation_support.py', 'experiments/investigations/__init__.py')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_files():
    paths = [path for path in ROOT.joinpath('src/yt_playlist').rglob('*')
             if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc']
    paths.extend(ROOT / name for name in (*BENCHMARKS,
        'scripts/audio_model/build_runtime.py', 'tests/__init__.py',
        'scripts/__init__.py', 'scripts/audio_model/__init__.py') if (ROOT / name).is_file())
    return sorted(paths)


def freeze():
    paths = runtime_files()
    expected = {str(path.relative_to(ROOT)): sha256(path) for path in paths}
    target = WORK / ('runtime-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    for source in paths:
        relative = str(source.relative_to(ROOT))
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected[relative]:
            raise ValueError('Source changed while freezing replay runtime: ' + relative)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    current = {str(path.relative_to(ROOT)): sha256(path) for path in runtime_files()}
    if current != expected:
        raise ValueError('Source tree changed while freezing replay runtime')
    manifest = {'files': expected, 'source_root': str(ROOT),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'policy': 'Exact working-tree source/assets plus benchmark helpers. Every write stays under experiments; this is a research runtime, not product acceptance.'}
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify(target)
    print(json.dumps({'runtime_snapshot': str(target), 'files': len(expected),
                      'manifest_sha256': sha256(target / 'manifest.json')}), flush=True)


def verify(target):
    target = Path(target).resolve()
    if not target.is_relative_to(WORK.resolve()):
        raise ValueError('Replay runtime must be inside experiments library-replay workspace')
    manifest = json.loads((target / 'manifest.json').read_text())
    for relative, expected in manifest['files'].items():
        path = (target / relative).resolve()
        if not path.is_relative_to(target) or sha256(path) != expected:
            raise ValueError('Frozen replay runtime changed: ' + relative)
    actual = {str(path.relative_to(target)) for path in target.rglob('*')
              if path.is_file() and path.name != 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('Frozen replay runtime contains unexpected or missing files')
    return manifest


def runtime_hashes():
    snapshot = os.environ.get(ENVIRONMENT)
    if not snapshot:
        paths = [*ROOT.joinpath('src/yt_playlist').rglob('*.py'),
                 *(ROOT / name for name in BENCHMARKS)]
        return {str(path.relative_to(ROOT)): sha256(path) for path in sorted(paths)}
    target = Path(snapshot).resolve()
    manifest = verify(target)
    for name, module in tuple(sys.modules.items()):
        if (name == 'yt_playlist' or name.startswith('yt_playlist.') or
                name in ('tests', 'experiments.investigations',
                         *(value[:-3].replace('/', '.') for value in BENCHMARKS))):
            path = getattr(module, '__file__', None)
            if path and not Path(path).resolve().is_relative_to(target):
                raise ValueError('Replay imported mutable production source: ' + name)
    return manifest['files']


def runtime_identity():
    snapshot = os.environ.get(ENVIRONMENT)
    return {'runtime_snapshot': snapshot,
            'runtime_snapshot_manifest_sha256': sha256(Path(snapshot) / 'manifest.json') if snapshot else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('module', choices=['experiments.audio_lab_tools.replay.library_replay',
                                         'experiments.audio_lab_tools.replay.output_evaluation'])
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    target = args.snapshot.resolve()
    verify(target)
    sys.path[:0] = [str(target / 'src'), str(target), str(ROOT)]
    os.environ[ENVIRONMENT] = str(target)
    sys.argv = [args.module, *args.args]
    runpy.run_module(args.module, run_name='__main__')


if __name__ == '__main__':
    main()
