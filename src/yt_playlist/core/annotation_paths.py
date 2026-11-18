"""Pure annotation file identities, independent of SQL and file access."""
from pathlib import Path


def record_path(root, identity):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Annotation file identity must be a positive integer')
    return Path(root) / 'by-id' / f'{identity % 256:02x}' / f'{identity}.json'


def record_id(filename):
    path = Path(filename)
    try:
        identity = int(path.stem)
        return identity if path.parts[-3:] == record_path('.', identity).parts[-3:] else None
    except ValueError:
        return None
