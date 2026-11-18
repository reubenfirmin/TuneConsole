"""Run in an isolated interpreter against extracted wheel bytes only."""
import json
import os
from pathlib import Path
import socket
import sys


def main():
    installed, input_file, app_home = sys.argv[1:]
    sys.path.insert(0, installed)
    os.environ['YT_PLAYLIST_HOME'] = app_home
    from yt_playlist.core import features
    features.LOCAL_ENV = Path(app_home) / 'absent.env'
    from yt_playlist.rec import audio_vibe_prediction, audio_vibe_specialists
    for _vibe, module in audio_vibe_specialists.SPECIALISTS:
        os.environ.pop(module.ENVIRONMENT_KEY, None)
        os.environ.pop(module.MODE_KEY, None)

    def forbidden(*args, **kwargs):
        raise AssertionError('Installed inference attempted network access')

    socket.socket.connect = forbidden
    cases = json.loads(Path(input_file).read_text())
    predictions = []
    for rows in cases:
        result = audio_vibe_prediction.predict(rows, require_complete=False)
        assert set(result['individual_models']) == {'cruise', 'release', 'relax', 'uplift', 'energize', 'concentrate'}
        assert result['vibe'] is None and not result['label']
        for vibe, candidate in result['individual_models'].items():
            assert result['suitability'][vibe] == candidate['score']
        predictions.append(result['individual_models'])
    assert all(module.__file__.startswith(installed) for _vibe, module in audio_vibe_specialists.SPECIALISTS)
    assert not any(key == 'experiments' or key.startswith('experiments.') for key in sys.modules)
    assert 'yt_playlist.rec.audio_vibe_model' not in sys.modules
    print(json.dumps({'module': audio_vibe_specialists.__file__,
        'sha256': result['individual_models']['concentrate']['sha256'], 'predictions': predictions}))


if __name__ == '__main__':
    main()
