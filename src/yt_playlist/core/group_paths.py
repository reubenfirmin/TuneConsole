"""Canonical paths for SQL-owned group documents."""
from pathlib import Path


def record_path(root, identity):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Invalid musical group file identity')
    return Path(root) / 'by-id' / f'{identity % 256:02x}' / f'{identity}.json'


def record_id(path):
    path = Path(path)
    if not path.stem.isdecimal():
        return None
    identity = int(path.stem)
    if identity <= 0:
        return None
    expected = record_path(Path(), identity)
    return identity if tuple(path.parts[-3:]) == expected.parts else None
