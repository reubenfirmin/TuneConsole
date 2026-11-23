"""Lossless previous-vector snapshots owned by a model selection run."""
from base64 import b64decode, b64encode
import json
import zlib


def pack(rows):
    encoded = [(key, b64encode(vector).decode('ascii')) for key, vector in rows]
    if not encoded:
        return None
    if len({key for key, _ in encoded}) != len(encoded):
        raise ValueError('Duplicate recording in model restore artifact')
    return zlib.compress(json.dumps(encoded, separators=(',', ':')).encode())


def unpack(value):
    if value is None:
        return []
    return [(key, b64decode(vector, validate=True)) for key, vector in json.loads(zlib.decompress(value))]
