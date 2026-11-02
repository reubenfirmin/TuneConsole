"""Build a wheel and verify Cruise works from its installed package alone."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

from yt_playlist.rec import audio_vibe_cruise, audio_vibe_model, audio_vibe_runtime
from .external_experiment import digest, save, stamp
from .vibe_specialist_round import inside


def check(directory):
    if directory.exists():
        raise ValueError('Choose a new packaging check directory')
    directory.mkdir(parents=True)
    subprocess.run(['uv', 'build', '--offline', '--wheel', '--out-dir', str(directory)], check=True)
    wheel, = directory.glob('*.whl')
    with TemporaryDirectory(prefix='wheel-', dir=directory) as installed:
        check_installed(directory, wheel, Path(installed))


def check_installed(directory, wheel, installed):
    with zipfile.ZipFile(wheel) as archive:
        for name, source in (
                ('yt_playlist/rec/audio_vibe_cruise.py', Path(audio_vibe_cruise.__file__)),
                ('yt_playlist/rec/audio_vibe_runtime.py', Path(audio_vibe_runtime.__file__)),
                ('yt_playlist/rec/data/cruise-vibes.json', audio_vibe_cruise.DEFAULT_PATH),
                ('yt_playlist/rec/data/emotify-vibes.json', audio_vibe_model.DEFAULT_PATH)):
            if archive.read(name) != source.read_bytes():
                raise ValueError('Wheel omitted or changed the production model: ' + name)
        archive.extractall(installed)
    # A fresh process imports from extracted wheel bytes. It cannot import the
    # checkout or experiments and must load the adjacent packaged JSON itself.
    program = '''import json, sys
sys.path.insert(0, sys.argv[1])
from yt_playlist.rec import audio_vibe_cruise as c
m = c.load(c.DEFAULT_PATH)
v = c.score(m, [[.2] * 200])
assert c.__file__.startswith(sys.argv[1])
assert not any(k == 'experiments' or k.startswith('experiments.') for k in sys.modules)
print(json.dumps({'module': c.__file__, 'sha256': m['sha256'], 'score': v['score']}))
'''
    result = subprocess.run([sys.executable, '-I', '-c', program, str(installed)],
                            cwd=installed, capture_output=True, text=True, check=True)
    report = {'created_at': stamp(), 'wheel': wheel.name, 'wheel_sha256': digest(wheel),
              'installed_runtime': json.loads(result.stdout), 'complete': True}
    save(directory / 'report.json', report)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    check(inside(args.directory))


if __name__ == '__main__':
    main()
