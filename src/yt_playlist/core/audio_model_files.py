"""Explicit bounded file/configuration access for optional audio models."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from yt_playlist.core import fao, features


def configured_value(name, default, environment, local_text):
    if name in environment:
        return environment[name]
    for line in local_text.splitlines():
        key, separator, value = line.strip().partition('=')
        if separator and key.strip() == name:
            return value.strip().strip('\"\'')
    return default


class AudioModelFiles:
    def __init__(self, environment, *, read=fao.read, configuration=None):
        self.environment, self.read = environment, read
        self.configuration = configuration or features.LOCAL_ENV

    def configured(self, name, default):
        # Environment overrides do not depend on a readable local file.
        if name in self.environment:
            return self.environment[name]
        try:
            with self.read(self.configuration, max_bytes=65536) as stream:
                local = stream.read(65537).decode()
            if len(local.encode()) > 65536:
                local = ''
        except (OSError, ValueError, UnicodeError):
            local = ''
        return configured_value(name, default, self.environment, local)

    def info(self, path):
        return self.read(path).info

    def load(self, path, size, maximum, unpack, *, exact_size=True):
        if size > maximum or (exact_size and size <= 0):
            raise ValueError('Audio-vibe artifact exceeds size bound')
        with self.read(path, max_bytes=maximum) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum or (exact_size and len(raw) != size):
            raise ValueError('Audio-vibe artifact changed or exceeds size bound')
        try:
            model = unpack(json.loads(raw))
        except RecursionError as error:
            raise ValueError('Invalid audio-vibe artifact nesting') from error
        return model, hashlib.sha256(raw).hexdigest()


def artifact_loader(unpack, maximum, *, exact_size=True, return_digest=False, cache_size=2, files=None):
    """One bounded cache per model, keyed by the physical file identity and logical size."""
    @lru_cache(maxsize=cache_size)
    def cached(path, identity, size):
        model, digest = (files or AudioModelFiles({})).load(path, size, maximum, unpack, exact_size=exact_size)
        if return_digest:
            return model, digest
        return {**model, 'sha256': digest}

    def load(path):
        path = str(Path(path).resolve())
        info = (files or AudioModelFiles({})).info(path)
        return cached(path, info.identity, info.logical_bytes)

    load.cache_clear = cached.cache_clear
    return load
