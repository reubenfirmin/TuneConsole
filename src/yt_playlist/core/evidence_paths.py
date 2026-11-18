"""Detached identities for immutable listening evidence files."""
from pathlib import Path

KINDS = frozenset(('opportunity', 'response', 'forecast', 'artifact', 'report', 'checkpoint'))
MAX_BYTES = 8 * 1024 * 1024


def record_key(kind, identity):
    if kind not in KINDS or not str(identity):
        raise ValueError('Invalid listening evidence identity')
    return kind, str(identity)


def record_path(root, kind, identity):
    if kind not in KINDS or type(identity) is not int or identity <= 0:
        raise ValueError('Invalid listening evidence file identity')
    return Path(root) / kind / f'{identity % 256:02x}' / f'{identity}.json'


def record_id(path):
    path = Path(path)
    if not path.stem.isdecimal() or int(path.stem) <= 0 or len(path.parts) < 3 or path.parts[-3] not in KINDS:
        return None
    identity = int(path.stem)
    return identity if tuple(path.parts[-3:]) == record_path(Path(), path.parts[-3], identity).parts else None


def maintenance_path(root, identity, name):
    if type(identity) is not int or identity != 1 or name not in ('inventory', 'retirements'):
        raise ValueError('Invalid listening evidence maintenance owner')
    return Path(root) / 'maintenance' / str(identity) / (name + '.json')
