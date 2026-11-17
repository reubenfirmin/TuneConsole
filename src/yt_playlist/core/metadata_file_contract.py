"""Detached identities and codecs for runtime metadata documents."""
import hashlib
from pathlib import Path

from .evidence_ledger import canonical

MAX_BYTES = 8 * 1024 * 1024
KINDS = frozenset(('alternatives', 'native', 'choices', 'conflicts'))


def owner_key(kind, owner):
    if kind not in KINDS or len(owner) != (2 if kind == 'alternatives' else 1):
        raise ValueError('Invalid metadata file owner')
    if any(not (type(value) is int and value > 0 or
                kind == 'native' and isinstance(value, str) and value) for value in owner):
        raise ValueError('Invalid metadata file owner')
    return canonical(list(owner)).decode()


def revision_key(revision):
    if type(revision) is int and revision >= 0:
        return str(revision)
    if type(revision) is float and revision == revision and abs(revision) < float('inf'):
        return revision.hex()
    raise ValueError('Invalid metadata file revision')


def record_path(root, identity):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Invalid metadata file identity')
    return Path(root) / 'by-id' / f'{identity % 256:02x}' / f'{identity}.json'


def record_id(path):
    path = Path(path)
    if not path.stem.isdecimal() or int(path.stem) <= 0:
        return None
    identity = int(path.stem)
    return identity if tuple(path.parts[-3:]) == record_path(Path(), identity).parts else None


def encode(kind, owner, revision, data):
    owner_key(kind, owner)
    revision_key(revision)
    body = {'format': 1, 'kind': kind, 'owner': list(owner), 'revision': revision, 'data': data}
    encoded = canonical({'body': body, 'sha256': hashlib.sha256(canonical(body)).hexdigest()})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Runtime metadata document exceeds its byte budget')
    return encoded


def describe(kind, owner, revision, data):
    encoded = encode(kind, owner, revision, data)
    return dict(kind=kind, owner=tuple(owner), revision=revision, data=data,
                digest=hashlib.sha256(encoded).hexdigest(), size=len(encoded))
