"""Pure file names for SQL-owned playlist captures."""
from pathlib import Path
import re


def record_path(root, identity):
    if not isinstance(identity, str) or re.fullmatch('[0-9a-f]{32}', identity) is None:
        raise ValueError('Playlist backup identity must be a SQL capture ID')
    return Path(root) / (identity + '.json')


def record_id(filename):
    path = Path(filename)
    try:
        return path.stem if path.name == record_path('.', path.stem).name else None
    except ValueError:
        return None
