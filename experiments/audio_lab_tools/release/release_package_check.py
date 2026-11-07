"""Verify Release and Cruise inference from an isolated, built wheel."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from yt_playlist.rec import audio_vibe_release as release
from ..support.external_experiment import digest, save, stamp
from ..focus.focus_experiment import inside
from .release_export import captures


def check(directory, wheel):
    if directory.exists():
        raise ValueError('Choose a new package check directory')
    directory.mkdir(parents=True)
    installed = directory / 'installed'
    root = Path(release.__file__).resolve().parents[1]
    names = ['rec/audio_vibe_runtime.py', 'rec/audio_vibe_release.py', 'rec/audio_vibe_cruise.py', 'rec/audio_vibe_model.py',
             'rec/audio_vibe_external.py', 'rec/audio_vibe_prediction.py', 'rec/vibe_preparation.py', 'rec/musical_character.py',
             'repos/vibe_predictions.py', 'core/audio_vibe_contract.py', 'rec/data/release-vibes.json',
             'rec/data/release-vibes.NOTICE.md', 'rec/data/cruise-vibes.json', 'rec/data/emotify-vibes.json']
    with zipfile.ZipFile(wheel) as archive:
        for name in names:
            if archive.read('yt_playlist/' + name) != (root / name).read_bytes():
                raise ValueError('Wheel omitted or changed a production file: ' + name)
        archive.extractall(installed)
    _metadata, rows = captures()[0]
    input_file = directory / 'captures.json'
    save(input_file, rows)
    expected = release.score(release.load(release.DEFAULT_PATH), [s['embedding']['values'] for s in rows])['score']
    program = '''import json, os, socket, sys
sys.path.insert(0, sys.argv[1])
os.environ['YT_PLAYLIST_HOME'] = sys.argv[3]
from yt_playlist.rec import audio_vibe_release as r, audio_vibe_cruise as c, audio_vibe_model as m
from yt_playlist.rec import audio_vibe_prediction as p
for module in (r, c, m):
    os.environ[module.ENVIRONMENT_KEY] = str(module.DEFAULT_PATH)
os.environ[r.MODE_KEY] = os.environ[c.MODE_KEY] = 'active'
def forbidden(*args, **kwargs):
    raise AssertionError('Installed inference attempted network access')
socket.socket.connect = forbidden
with open(sys.argv[2]) as stream:
    rows = json.load(stream)
result = p.predict(rows, require_complete=False)
assert r.__file__.startswith(sys.argv[1])
assert not any(k == 'experiments' or k.startswith('experiments.') for k in sys.modules)
assert set(result['individual_models']) == {'cruise', 'release'}
print(json.dumps({'module': r.__file__, 'sha256': result['individual_models']['release']['sha256'],
                  'release_score': result['suitability']['release'], 'cruise_score': result['suitability']['cruise']}))
'''
    result = subprocess.run([sys.executable, '-I', '-c', program, str(installed), str(input_file),
                             str(directory / 'app-home')], cwd=installed, capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    if abs(observed['release_score'] - expected) > 1e-12:
        raise ValueError('Installed inference differs')
    report = {'created_at': stamp(), 'wheel': wheel.name, 'wheel_sha256': digest(wheel),
              'installed_runtime': observed, 'checked_files': {name: digest(root / name) for name in names},
              'input_sha256': digest(input_file), 'complete': True}
    save(directory / 'report.json', report)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    args = parser.parse_args()
    check(inside(args.directory), inside(args.wheel))


if __name__ == '__main__':
    main()
