"""Verify the six default specialists in an isolated built wheel."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release
from yt_playlist.rec import audio_vibe_relax as relax, audio_vibe_uplift as uplift
from yt_playlist.rec import audio_vibe_energize as energize, audio_vibe_focus as focus
from .external_experiment import digest, save, stamp
from .focus_experiment import inside
from .focus_export import captures


def check(directory, wheel):
    if directory.exists():
        raise ValueError('Choose a new package check directory')
    directory.mkdir(parents=True)
    with TemporaryDirectory(prefix='wheel-', dir=directory) as installed:
        return check_installed(directory, wheel, Path(installed))


def check_installed(directory, wheel, installed):
    root = Path(focus.__file__).resolve().parents[1]
    names = ['rec/audio_vibe_runtime.py', 'rec/audio_vibe_model.py', 'rec/audio_vibe_external.py', 'rec/audio_vibe_prediction.py',
             'rec/vibe_preparation.py', 'rec/musical_character.py', 'repos/vibe_predictions.py',
             'core/audio_vibe_contract.py', 'rec/data/emotify-vibes.json']
    for name in ('cruise', 'release', 'relax', 'uplift', 'energize', 'focus'):
        names.extend([f'rec/audio_vibe_{name}.py', f'rec/data/{name}-vibes.json'])
        notice = f'rec/data/{name}-vibes.NOTICE.md'
        if (root / notice).exists():
            names.append(notice)
    with zipfile.ZipFile(wheel) as archive:
        for name in names:
            if archive.read('yt_playlist/' + name) != (root / name).read_bytes():
                raise ValueError('Wheel omitted or changed a production file: ' + name)
        archive.extractall(installed)
    cases = [samples[:count] for _metadata, samples in captures()
             for count in sorted({1, min(3, len(samples)), len(samples)})]
    input_file = directory / 'captures.json'
    save(input_file, cases)
    modules = {'cruise': cruise, 'release': release, 'relax': relax, 'uplift': uplift,
               'energize': energize, 'concentrate': focus}
    expected = [{vibe: {**module.score(module.load(module.DEFAULT_PATH),
                    [s['embedding']['values'] for s in case]), 'mode': 'active'}
                 for vibe, module in modules.items()} for case in cases]
    program = '''import json, os, socket, sys
sys.path.insert(0, sys.argv[1])
os.environ['YT_PLAYLIST_HOME'] = sys.argv[3]
from yt_playlist.core import features
features.LOCAL_ENV = features.LOCAL_ENV.with_name('absent-focus-check.env')
from yt_playlist.rec import audio_vibe_focus as f, audio_vibe_energize as n, audio_vibe_uplift as u, audio_vibe_relax as r
from yt_playlist.rec import audio_vibe_release as e, audio_vibe_cruise as c, audio_vibe_model as m
from yt_playlist.rec import audio_vibe_prediction as p
for module in (f, n, u, r, e, c, m):
    os.environ.pop(module.ENVIRONMENT_KEY, None)
for module in (f, n, u, r, e, c):
    os.environ.pop(module.MODE_KEY, None)
def forbidden(*args, **kwargs):
    raise AssertionError('Installed inference attempted network access')
socket.socket.connect = forbidden
with open(sys.argv[2]) as stream:
    cases = json.load(stream)
predictions = []
for rows in cases:
    result = p.predict(rows, require_complete=False)
    assert set(result['individual_models']) == {'cruise', 'release', 'relax', 'uplift', 'energize', 'concentrate'}
    for vibe, candidate in result['individual_models'].items():
        assert result['suitability'][vibe] == candidate['score']
    predictions.append(result['individual_models'])
assert f.__file__.startswith(sys.argv[1])
assert not hasattr(c, 'route') and not hasattr(c, 'route_samples')
assert not any(k == 'experiments' or k.startswith('experiments.') for k in sys.modules)
print(json.dumps({'module': f.__file__, 'sha256': result['individual_models']['concentrate']['sha256'], 'predictions': predictions}))
'''
    result = subprocess.run([sys.executable, '-I', '-c', program, str(installed), str(input_file),
                             str(directory / 'app-home')], cwd=installed, capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    if observed.pop('predictions') != expected:
        raise ValueError('Installed inference differs')
    report = {'created_at': stamp(), 'wheel': wheel.name, 'wheel_sha256': digest(wheel),
        'installed_runtime': observed, 'checked_files': {name: digest(root / name) for name in names},
        'input_sha256': digest(input_file), 'cases': len(expected), 'specialists': list(modules),
        'maximum_error': 0., 'exact_native_and_provenance_equality': True,
        'default_modes': True, 'network_blocked': True, 'complete': True}
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
