"""Profile pack paths derived from committed SQL receipt IDs."""
from pathlib import Path


def record_path(kind, identity):
    if kind not in ('recording', 'source') or type(identity) is not int or identity <= 0:
        raise ValueError('Invalid profile file identity')
    return Path(kind, 'by-id', f'{identity % 256:02x}', f'{identity}.json')


def record_id(path):
    path = Path(path)
    if len(path.parts) < 4 or not path.stem.isdecimal():
        return None
    identity = int(path.stem)
    try:
        expected = record_path(path.parts[-4], identity)
    except ValueError:
        return None
    return identity if tuple(path.parts[-4:]) == expected.parts else None
