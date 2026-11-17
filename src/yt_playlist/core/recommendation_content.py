"""Bounded recommendation documents and ID paths, with no database access."""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
from threading import Lock

from . import fao
from .document_paths import record_id, record_path
from .evidence_ledger import canonical

FORMAT = 'prepared-recommendation-output-1'
MAX_BYTES = 1024 * 1024
_HOME_SURFACES = frozenset({'home_prepared', 'home_tuned', 'home_choice'})
_HOME_CACHE_BYTES = 16 * 1024 * 1024
_home_parts = OrderedDict()
_home_parts_lock = Lock()


def decode_json(raw):
    def invalid(value):
        raise ValueError('Non-finite number in prepared output')
    def number(value):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else invalid(value)
    return json.loads(raw, parse_float=number, parse_constant=invalid)


def encode(surface, chunk_key, value):
    if (not isinstance(surface, str) or not surface or len(surface.encode()) > 1024
            or not isinstance(chunk_key, str) or len(chunk_key.encode()) > 1024):
        raise ValueError('Invalid prepared output owner')
    encoded = canonical({'format': FORMAT, 'surface': surface, 'chunk_key': chunk_key, 'output': value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Prepared output exceeds its chunk byte budget')
    return encoded


def decode(encoded, surface=None, chunk_key=None):
    document = json.loads(encoded)
    if (not isinstance(document, dict) or set(document) != {'format', 'surface', 'chunk_key', 'output'}
            or document['format'] != FORMAT or not isinstance(document['surface'], str) or not document['surface']
            or not isinstance(document['chunk_key'], str) or canonical(document) != encoded
            or surface is not None and document['surface'] != surface
            or chunk_key is not None and document['chunk_key'] != chunk_key):
        raise ValueError('Unsupported prepared output document or owner')
    return document


def describe(encoded):
    if not isinstance(encoded, bytes) or not 0 < len(encoded) <= MAX_BYTES:
        raise ValueError('Invalid prepared output bytes')
    decode(encoded)
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def read_encoded(filename, digest, size, *, surface=None, chunk_key=None, with_age=False, validate_document=True):
    if type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid prepared output byte range')
    path = Path(filename)
    with fao.read(path) as stream:
        age = stream.info
        encoded = stream.read(size + 1)
    if ((record_id(path) is None and path.stem != digest)
            or len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest):
        raise ValueError('Prepared output does not match its digest')
    if validate_document:
        decode(encoded, surface, chunk_key)
    return (encoded, age) if with_age else encoded


def _file_identity(filename):
    observed = Path(filename).stat()
    return (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)


def read(filename, digest, size, *, surface, chunk_key):
    if type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid prepared output byte range')
    def output():
        return decode(read_encoded(filename, digest, size, validate_document=False), surface, chunk_key)['output']
    if surface not in _HOME_SURFACES:
        return output()
    identity = _file_identity(filename)
    key = (str(filename), digest, size, surface, chunk_key, identity)
    with _home_parts_lock:
        cached = _home_parts.get(key)
        if cached is not None:
            _home_parts.move_to_end(key)
            return cached[0]
    value = output()
    if isinstance(value, str) and _file_identity(filename) == identity:
        with _home_parts_lock:
            _home_parts[key] = (value, len(value.encode()))
            while (len(_home_parts) > 128
                    or sum(item[1] for item in _home_parts.values()) > _HOME_CACHE_BYTES):
                _home_parts.popitem(last=False)
    return value
