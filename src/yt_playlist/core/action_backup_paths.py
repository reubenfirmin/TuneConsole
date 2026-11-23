"""Paths for retained playlist backups."""
from pathlib import Path


def record_path(root, identity):
    if type(identity) is int and identity > 0:
        shard = f'{identity % 256:02x}'
    elif isinstance(identity, str) and len(identity) == 32 and all(c in '0123456789abcdef' for c in identity):
        shard = identity[:2]
    else:
        raise ValueError('Invalid action backup file ID')
    return Path(root) / 'backups' / shard / f'{identity}.json'


def record_id(path):
    path = Path(path)
    if path.parent.parent.name != 'backups':
        return None
    stem = path.stem
    identity = int(stem) if len(stem) < 32 and stem.isascii() and stem.isdecimal() else stem
    try:
        expected = record_path(path.parent.parent.parent, identity)
    except ValueError:
        return None
    return identity if path == expected else None
