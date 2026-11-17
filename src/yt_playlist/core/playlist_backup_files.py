"""Durable playlist snapshot files; no provider or repository decisions."""
import json
import hashlib
from contextlib import contextmanager

from yt_playlist.core import fao, paths
from yt_playlist.core.playlist_backup_paths import record_path

MAX_BYTES = 64 * 1024 * 1024


def _encode(payload):
    encoded = json.dumps(payload, indent=2).encode()
    if not encoded or len(encoded) > MAX_BYTES:
        raise ValueError('Playlist backup exceeds its byte budget')
    return encoded


class PlaylistBackupFiles:
    def __init__(self, root=None, *, read=None):
        self.root = root
        self.read = read or fao.read

    @staticmethod
    def describe(payload):
        encoded = _encode(payload)
        return hashlib.sha256(encoded).hexdigest(), len(encoded)

    @contextmanager
    def write_reserved(self, identity, payload, *, expected):
        name = record_path('.', identity).name
        path = (self.root if self.root is not None else paths.backups_dir()) / name
        encoded = _encode(payload)
        integrity = hashlib.sha256(encoded).hexdigest(), len(encoded)
        if integrity != expected:
            raise ValueError('Playlist backup changed after reservation')
        with fao.prepare(path, fao.Content(encoded, durable=True, max_bytes=MAX_BYTES)) as pending:
            pending.publish()
            yield str(path), *integrity

    def load(self, filename):
        # Recovery may refer to a retained content-addressed copy outside the capture root.
        return json.loads(self.read(filename, max_bytes=MAX_BYTES).bytes)
