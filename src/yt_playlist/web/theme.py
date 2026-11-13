"""Theme lookups for server-rendered colour.

Genre-family colour tokens (`family_hue`, `genre_tint`) live in `util.genre_theme`, a neutral
module, and are re-exported here: both web presenters and detached `rec/` modules need them, and
lower business code must not import its web consumer.
"""

from functools import lru_cache
from pathlib import Path
import re

from yt_playlist.core import fao
from yt_playlist.util.genre_theme import family_hue, genre_tint  # noqa: F401 (re-exported)


def mix_launch_tokens() -> str:
    """Embed recovery roles and their palette so recovery needs no asset request.

    Keep the values in tokens.css, including when a popup survives its opener
    or a failed stylesheet load. Only this small dependency closure is embedded.
    """
    path = Path(__file__).with_name('static') / 'tokens.css'
    return _mix_launch_tokens(fao.read(path).info.identity)


@lru_cache(maxsize=1)
def _mix_launch_tokens(identity: tuple) -> str:
    css = fao.read(Path(__file__).with_name('static') / 'tokens.css').bytes.decode()
    definitions = dict(re.findall(r'(--[a-z0-9-]+)\s*:\s*([^;{}]+);', css))
    selected = {}

    def include(name):
        if name in selected:
            return
        value = definitions[name]
        selected[name] = value
        for dependency in re.findall(r'var\((--[a-z0-9-]+)', value):
            include(dependency)

    for name in definitions:
        if name.startswith('--mix-launch-'):
            include(name)
    return ':root{' + ''.join(f'{name}:{value};' for name, value in selected.items()) + '}'
