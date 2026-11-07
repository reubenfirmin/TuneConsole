"""Artifact naming and historical source verification for offline research."""
import hashlib
from pathlib import Path


LEGACY_PROTOCOLS = Path(__file__).resolve().parents[2] / 'audio_lab/local/protocols'


def legacy_protocol(directory, filename, expected):
    """Verify a historical document from its source snapshot or local archive."""
    if Path(filename).name != filename or not filename.endswith('.md'):
        raise ValueError('Expected a historical protocol filename')
    for path in (Path(directory) / 'source/audio_lab' / filename, LEGACY_PROTOCOLS / filename):
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Archived protocol changed: ' + str(path))
            return path
    raise FileNotFoundError('Historical protocol is absent from local artifacts: ' + filename)


def compact_slug(value):
    if len(value.encode()) <= 220:
        return value
    prefix = value.encode()[:96].decode(errors='ignore')
    return prefix + '__' + hashlib.sha256(value.encode()).hexdigest()[:24]
