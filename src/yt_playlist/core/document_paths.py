"""Canonical numeric-ID JSON paths, with no file or database access."""
from pathlib import Path


def record_path(root, identity):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Invalid document ID')
    return Path(root) / 'by-id' / f'{identity % 256:02x}' / f'{identity}.json'


def record_id(path):
    path = Path(path)
    if path.parent.parent.name != 'by-id' or not path.stem.isascii() or not path.stem.isdecimal():
        return None
    identity = int(path.stem)
    if identity <= 0 or path.parent.name != f'{identity % 256:02x}' or path.name != f'{identity}.json':
        return None
    return identity
