"""Immutable bounded model bytes. All SQL lives in repos.vibe_models."""
import hashlib
import json
import math
import re
from pathlib import Path

from . import fao
from .evidence_ledger import canonical

MAX_BYTES = 16 * 1024 * 1024
HEAD_PREFIX = 'rec_vibe_joint_head_'
STATE_PREFIX = 'rec_vibe_joint_state_'
REVISION = 'rec_vibe_joint_revision'
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def relative(generation):
    if not isinstance(generation, str) or not _DIGEST.fullmatch(generation):
        raise ValueError('Invalid shared vibe generation identity')
    return Path(generation[:2], generation + '.json')


def encode(value):
    raw = canonical(value)
    if len(raw) > MAX_BYTES:
        raise fao.FileBudgetExceeded('Shared vibe generation exceeds artifact budget')
    return hashlib.sha256(raw).hexdigest(), raw


def read(root, generation):
    try:
        raw = fao.read(root / relative(generation), max_bytes=MAX_BYTES).bytes
    except FileNotFoundError as exc:
        raise fao.CorruptFile('Required shared vibe generation is missing') from exc
    if hashlib.sha256(raw).hexdigest() != generation:
        raise fao.CorruptFile('Shared vibe generation differs from its identity')
    try:
        value = json.loads(raw)
        rows = value['inputs']
        if (value['format'] not in (1, 2, 3) or not isinstance(value['vibe'], str) or not isinstance(rows, list)
                or not rows or len(rows) > (1024 if value['format'] == 1 else 32768)
                or len({row['recording'] for row in rows}) != len(rows)
                or len({row['measurement'] for row in rows}) != len(rows)
                or not isinstance(value['configuration'], dict)
                or any(type(row['measurement']) is not int or row['measurement'] < 1
                       or type(row['recording']) is not int or row['recording'] < 1
                       or not isinstance(row['artists'], list)
                       or row['artists'] != sorted(set(row['artists']))
                       or any(type(artist) is not int or artist < 1 for artist in row['artists'])
                       or not math.isfinite(row['score']) or not 0 <= row['score'] <= 1
                       or not math.isfinite(row['support']) or not 0 < row['support'] <= 1
                       or any(not isinstance(row[key], str) or not row[key]
                              for key in ('source','version','input_digest'))
                       for row in rows)):
            raise ValueError('Invalid shared training manifest')
    except (KeyError, TypeError, ValueError) as exc:
        raise fao.CorruptFile('Invalid shared vibe generation') from exc
    return value


def stage(root, value):
    generation, raw = encode(value)
    path = root / relative(generation)
    source = fao.read(path, max_bytes=MAX_BYTES) if path.is_file() else None
    if source is not None:
        try:
            if source.bytes == raw:
                return generation, None
        except (OSError, ValueError):
            pass
    return generation, fao.prepare(path, source.replacing(raw) if source is not None else raw)


def measurement_ids(value):
    return [row['measurement'] for row in value['inputs']]


def receipt(generation, artists, mean_variance, new_track_variance, *, conditioning=None):
    relative(generation)
    if (not isinstance(mean_variance, (int, float)) or not isinstance(new_track_variance, (int, float))
            or not 0 <= mean_variance <= new_track_variance < float('inf')):
        raise ValueError('Invalid joint vibe uncertainty')
    value = {'generation': generation, 'artists': sorted(set(artists)),
             'mean_variance': mean_variance, 'new_track_variance': new_track_variance}
    if conditioning is not None:
        if (not isinstance(conditioning, dict) or set(conditioning) != {'genre', 'album'}
                or any(item is not None and (not isinstance(item, str) or not item)
                       for item in conditioning.values()) or len(canonical(conditioning)) > 2048):
            raise ValueError('Invalid joint target conditioning')
        value['conditioning'] = dict(conditioning)
    return value


def is_receipt(value):
    fields = {'generation', 'artists', 'mean_variance', 'new_track_variance'}
    return isinstance(value, dict) and set(value) in (fields, fields | {'conditioning'})
