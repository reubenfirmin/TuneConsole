"""File-backed checked editorial catalog; callers receive defensive copies."""
import json
from pathlib import Path
from functools import lru_cache

from yt_playlist.core import fao


class EditorialCatalog:
    def __init__(self, path=None, *, read=fao.read):
        self.path = path or Path(__file__).parent.parent / 'data' / 'album_editorial.json'
        self.read = read

    @lru_cache(maxsize=1)
    def _load(self):
        return json.loads(self.read(self.path).bytes)['entries']

    def entries(self):
        return json.loads(json.dumps(self._load()))
