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
from yt_playlist.rec import audio_vibe_arc
from ..support.external_experiment import digest, save, stamp
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
    names = ['rec/audio_vibe_runtime.py', 'rec/audio_vibe_specialists.py', 'rec/audio_vibe_arc.py', 'rec/audio_vibe_prediction.py',
             'rec/audio_vibe_values.py',
             'rec/vibe_preparation.py', 'rec/musical_character.py', 'repos/vibe_predictions.py',
             'core/audio_vibe_contract.py', 'rec/vibe_intensity.py',
             'controllers/vibe_prediction.py', 'library/vibe_composition.py']
    for name in ('cruise', 'release', 'relax', 'uplift', 'energize', 'focus'):
        names.extend([f'rec/audio_vibe_{name}.py', f'rec/data/{name}-vibes.json'])
        notice = f'rec/data/{name}-vibes.NOTICE.md'
        if (root / notice).exists():
            names.append(notice)
    with zipfile.ZipFile(wheel) as archive:
        retired = {'yt_playlist/rec/audio_vibe_model.py', 'yt_playlist/rec/audio_vibe_external.py',
                   'yt_playlist/rec/data/emotify-vibes.json'}
        if retired & set(archive.namelist()) or any(name.startswith('experiments/') for name in archive.namelist()):
            raise ValueError('Wheel includes retired research code or artifacts')
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
    expected = [{vibe: {**audio_vibe_arc.specialist(case, module.load(module.DEFAULT_PATH), module.score, vibe), 'mode': 'active'}
                 for vibe, module in modules.items()} for case in cases]
    probe = Path(__file__).with_name('installed_specialists_check.py')
    result = subprocess.run([sys.executable, '-I', str(probe), str(installed), str(input_file),
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
