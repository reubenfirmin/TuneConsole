"""Retained playlist backup paths derived from their SQL file record IDs."""
from pathlib import Path


def record_path(root, identity):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Invalid action backup file ID')
    return Path(root) / 'backups' / f'{identity % 256:02x}' / f'{identity}.json'


def record_id(path):
    path = Path(path)
    if path.parent.parent.name != 'backups' or not path.stem.isascii() or not path.stem.isdecimal():
        return None
    identity = int(path.stem)
    if identity <= 0 or path != record_path(path.parent.parent.parent, identity):
        return None
    return identity
