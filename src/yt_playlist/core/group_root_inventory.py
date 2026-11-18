"""Explicit bounded file inventory capability for immutable listening evidence."""
import hashlib
from yt_playlist.core import fao


class GroupRootInventory:
    def __init__(self, ledger):
        self.ledger = ledger

    def paths(self, kind):
        return iter((self.ledger.directory / kind).glob('*/*.json'))

    def size(self, path):
        return fao.read(path).info.logical_bytes

    def read(self, path):
        return fao.read(path).bytes

    def get(self, kind, identity):
        return self.ledger.get(kind, identity)

    def owns(self, kind, identity, path):
        return hashlib.sha256(identity.encode()).hexdigest() == path.stem
