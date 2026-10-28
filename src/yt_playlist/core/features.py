"""Environment switches for product previews."""
import os
from pathlib import Path


LOCAL_ENV = Path(__file__).resolve().parents[3] / '.env.local'


def listening_charts_enabled():
    return os.environ.get('YT_PLAYLIST_LISTENING_CHARTS', '1') == '1'


def audio_vibes_enabled():
    """Developer vibe-rating UI; audio collection runs independently of this switch.

    An explicit environment value overrides the local file.

    Read only this switch, without importing secrets or changing process environment.
    The checkout's ignored .env.local also works with the ordinary app launch command.
    """
    key = 'YT_PLAYLIST_AUDIO_VIBES'
    if key in os.environ:
        return os.environ[key] == '1'
    try:
        for line in LOCAL_ENV.read_text().splitlines():
            name, separator, value = line.strip().partition('=')
            if separator and name.strip() == key:
                return value.strip().strip('\"\'') == '1'
    except OSError:
        pass
    return False
