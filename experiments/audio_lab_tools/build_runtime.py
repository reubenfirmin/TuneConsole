"""Explicit local research build; never invoked by installation or normal tests.

Requires pinned Essentia and emsdk checkouts plus Eigen 3.3.7 headers. All build
products stay in those work directories or ignored vendor/. No system install.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1] / 'audio_lab'
ESSENTIA = 'd65623a92ab1d26803c5d8656a3ad7c5b2929031'
EMSDK = '0b3bcbc3b005cbb811d48e497eabcc6846d43001'
EIGEN_HEADERS = '40801d16b0a1f627f14ae295280cd520a759cfac25afbb50b9deb61f580e53b6'
ALGORITHMS = ','.join((
    'TensorflowInputMusiCNN', 'Windowing', 'Spectrum', 'Magnitude', 'MelBands', 'TriangularBands',
    'UnaryOperator', 'FFT', 'IFFTK', 'RhythmExtractor2013', 'BeatTrackerDegara',
    'BeatTrackerMultiFeature', 'CartesianToPolar', 'OnsetDetection', 'Flux', 'HFC',
    'TempoTapDegara', 'AutoCorrelation', 'FrameCutter', 'MovingAverage', 'IIR',
    'OnsetDetectionGlobal', 'ERBBands', 'Scale', 'TempoTapMaxAgreement', 'NoiseAdder',
    'LoudnessEBUR128', 'LoudnessEBUR128Filter', 'StereoDemuxer', 'BinaryOperatorStream',
    'UnaryOperatorStream', 'Mean',
))
FLAGS = ['--bind', '-Oz', '-sDYNAMIC_EXECUTION=0', '-sMODULARIZE=1',
         '-sEXPORT_NAME=EssentiaCSP', '-sENVIRONMENT=worker', '-sSINGLE_FILE=1',
         '-sALLOW_MEMORY_GROWTH=1', '-sDISABLE_EXCEPTION_CATCHING=0']


@contextmanager
def machine_lock():
    """Coordinate a resource-heavy build with the repository's pytest owner."""
    path = Path(f'/tmp/tuneconsole-tests-{os.getuid()}.lock')
    with path.open('a+') as owner, path.with_name(path.stem + '-workers.lock').open('a+') as workers:
        deadline = time.monotonic() + 60
        announced = False
        while True:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(workers, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                fcntl.flock(owner, fcntl.LOCK_UN)
                owner.seek(0)
                details = owner.read()
                if not announced:
                    print('Waiting up to 60s for test/build owner: ' + details, flush=True)
                    announced = True
                if time.monotonic() >= deadline:
                    raise SystemExit('Machine test/build lock remains busy; retry later.')
                time.sleep(.1)
        owner.seek(0)
        owner.truncate()
        json.dump({'pid': os.getpid(), 'cwd': str(Path.cwd()),
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'command': shlex.join([sys.executable, *sys.argv])}, owner)
        owner.flush()
        # Keep the lifetime barrier in the build subprocess if this coordinator
        # exits unexpectedly, just as pytest does for its workers.
        yield workers.fileno()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--emsdk', required=True, type=Path)
    parser.add_argument('--eigen', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable, help='Python compatible with upstream waf (3.12 tested)')
    args = parser.parse_args()
    source, sdk, eigen = (p.resolve() for p in (args.source, args.emsdk, args.eigen))
    headers = hashlib.sha256()
    for path in sorted([*(eigen / 'Eigen').rglob('*'), *(eigen / 'unsupported/Eigen').rglob('*')]):
        if path.is_file():
            headers.update(path.relative_to(eigen).as_posix().encode() + b'\0')
            headers.update(hashlib.sha256(path.read_bytes()).digest())
    if headers.hexdigest() != EIGEN_HEADERS:
        raise SystemExit('Eigen headers differ from the pinned 3.3.7 archive')
    if not (ROOT / 'vendor/bundle.json').exists():
        raise SystemExit('Fetch the local research bundle first with fetch_model.py')
    for checkout, revision in [(source, ESSENTIA), (sdk, EMSDK)]:
        actual = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != revision:
            raise SystemExit(f'Unexpected source revision: {checkout}: {actual}')
        if subprocess.check_output(['git', '-C', str(checkout), 'diff', 'HEAD', '--'], text=True).strip():
            raise SystemExit(f'Tracked source changes in {checkout}; use a clean pinned checkout')
    compiler = sdk / 'upstream/emscripten'
    env = {**os.environ, 'EM_CONFIG': str(sdk / '.emscripten'),
           'EMSCRIPTEN': str(compiler), 'EMCC_CORES': '2',
           'CC': str(compiler / 'emcc'), 'CXX': str(compiler / 'em++'),
           'AR': str(compiler / 'emar'), 'CXXFLAGS': '-fexceptions',
           'PATH': str(compiler) + os.pathsep + os.environ['PATH']}

    def run(command, **kwargs):
        print(shlex.join(map(str, command)), flush=True)
        subprocess.run(list(map(str, command)), check=True, env=env, pass_fds=(lifetime,), **kwargs)

    with machine_lock() as lifetime:
        version = subprocess.check_output([str(compiler / 'em++'), '--version'], env=env, text=True)
        if '3.1.64' not in version.splitlines()[0]:
            raise SystemExit('Activate emsdk 3.1.64 before building')
        pkg = source / 'audio-lab-pkgconfig'
        pkg.mkdir(exist_ok=True)
        (pkg / 'eigen3.pc').write_text(f'Name: Eigen3\nDescription: Eigen headers\nVersion: 3.3.7\nCflags: -I{eigen}\n')
        run([args.python, 'waf', 'configure', '--build-static', '--fft=KISS', '--emscripten',
             '--lightweight=', '--ignore-algos=Spline,CubicSpline,NNLSChroma,SNR',
             '--include-algos=' + ALGORITHMS, '--pkg-config-path=' + str(pkg)], cwd=source)
        run([args.python, 'waf', 'build', '-j2'], cwd=source)
        vendor = ROOT / 'vendor'
        output = vendor / 'essentia-csp.js'
        run([compiler / 'em++', *FLAGS, '-std=c++11', '-I' + str(eigen),
             '-I' + str(source / 'src'), '-I' + str(source / 'src/essentia'),
             '-I' + str(source / 'src/essentia/utils'), '-I' + str(source / 'src/3rdparty'),
             ROOT / 'essentia_csp.cpp', source / 'build/src/libessentia.a', '-o', output])
        data = output.read_bytes()
        manifest = {'schema': 1, 'purpose': 'local_research_only', 'runtime': 'essentia-csp-audio-2',
                    'essentia_commit': ESSENTIA, 'emsdk_commit': EMSDK, 'emscripten': '3.1.64',
                    'eigen': '3.3.7', 'algorithms': ALGORITHMS.split(','), 'flags': FLAGS,
                    'eigen_headers_sha256': EIGEN_HEADERS,
                    'core_cxxflags': ['-std=c++11', '-Oz', '-fexceptions'], 'fft': 'KISS',
                    'binding_sha256': hashlib.sha256((ROOT / 'essentia_csp.cpp').read_bytes()).hexdigest(),
                    'files': [{'path': output.name, 'bytes': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()}]}
        (vendor / 'runtime.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'Built {output}: {len(data)} bytes', flush=True)


if __name__ == '__main__':
    main()
