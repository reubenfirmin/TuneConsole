"""Immutable collection documents; file access has no database dependency."""
import hashlib
import json
from pathlib import Path

from . import fao, file_collection
from .document_paths import record_id, record_path
from .evidence_ledger import canonical

FORMAT = 'collection-recovery-1'
KINDS = frozenset({'road-trip-draft', 'radio-session', 'library-sync', 'taste-settings',
    'taste-checkpoint', 'taste-comparison', 'experiment-decision', 'spotify-import',
    'playlist-recipe', 'playlist-canvas', 'action', 'action-draft', 'action-step',
    'playlist-gc', 'spotify-transfer', 'tv-append', 'discovery-candidate'})
MAX_BYTES = 16 * 1024 * 1024


def encode(kind, owner, value):
    if kind not in KINDS:
        raise ValueError('Unsupported collection recovery owner')
    encoded = canonical({'format': FORMAT, 'kind': kind, 'owner': str(owner), 'state': value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Collection recovery exceeds its byte budget')
    return encoded


class RecoveryFiles:
    def __init__(self, root):
        self.root = root

    def retained(self):
        return file_collection.retained_reader(self.root)

    def describe(self, kind, owner, value):
        encoded = encode(kind, owner, value)
        return hashlib.sha256(encoded).hexdigest(), len(encoded)

    def publish_reserved(self, identity, kind, owner, value, *, expected, age=None):
        encoded = encode(kind, owner, value)
        integrity = hashlib.sha256(encoded).hexdigest(), len(encoded)
        if integrity != expected:
            raise ValueError('Collection document changed after reservation')
        path = record_path(self.root, identity)
        fao.write(path, fao.Content(encoded, max_bytes=MAX_BYTES, metadata=age))
        return str(path), *integrity

    def read(self, reference, *, kind, owner):
        return read(*reference, kind=kind, owner=owner)


def read(filename, digest, size, *, kind, owner, with_age=False):
    if kind not in KINDS or type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid collection recovery reference')
    path = Path(filename)
    native = record_id(path) is not None
    with fao.read(path) as stream:
        age = stream.info
        encoded = stream.read(size + 1)
    if ((not native and path.stem != digest) or len(encoded) != size
            or hashlib.sha256(encoded).hexdigest() != digest):
        raise ValueError('Collection recovery does not match its digest')
    document = json.loads(encoded)
    if (not isinstance(document, dict) or set(document) != {'format', 'kind', 'owner', 'state'}
            or document['format'] != FORMAT or document['kind'] != kind or document['owner'] != str(owner)
            or canonical(document) != encoded):
        raise ValueError('Collection recovery belongs to another workflow or format')
    return (document['state'], age) if with_age else document['state']
