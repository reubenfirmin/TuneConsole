"""Fixed report publications, with serving decisions held in typed relations.

The files contain bounded diagnostic output. Home format selection and refresh
admission read SQL fields and never depend on decoding a diagnostic document.
"""
from dataclasses import dataclass
import hashlib
import json
import math

from . import recommendation_outputs as files
from . import publication_files

SURFACES = frozenset({'listening_choices', 'choice_daily', 'model_health'})
FORMATS = ('mixes', 'albums', 'radio')
MAX_BYTES = 4 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
PREFIX = publication_files.REPORT
METADATA = ('built_at', 'observed_at', 'schema_version', 'watermark', 'report_key', 'evidence_generation',
            'inputs_key', 'next_at', 'vibe_episodes', 'half_life_days', 'content_digest', 'total_bytes', 'has_content')


def _number(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Invalid report number')
    return value


def _identity(value):
    if value is not None and (not isinstance(value, str) or not 0 < len(value.encode()) <= 1024):
        raise ValueError('Invalid report identity')
    return value


@dataclass(frozen=True)
class Prepared:
    surface: str
    raw: str
    parts: tuple
    metadata: tuple
    usage: tuple


def prepare(surface, raw, at):
    if surface not in SURFACES or not isinstance(raw, str) or not 0 < len(raw.encode()) <= MAX_BYTES:
        raise ValueError('Invalid report owner or byte budget')
    value = files.decode_json(raw)
    if not isinstance(value, dict):
        raise ValueError('Report output must be an object')
    refresh = value.get('refresh') or {}
    if not isinstance(refresh, dict):
        raise ValueError('Invalid report refresh admission')
    schema = value.get('schema', value.get('version'))
    if schema is not None and (type(schema) is not int or schema < 0):
        raise ValueError('Invalid report schema version')
    usage = (value.get('home_format') or {}) if surface == 'listening_choices' else {}
    if (not isinstance(usage, dict) or set(usage) - {'scores', 'episodes', 'days', 'vibe_episodes', 'half_life_days'}):
        raise ValueError('Invalid Home format usage')
    rows = []
    for field in ('scores', 'episodes', 'days'):
        data = usage.get(field) or {}
        if not isinstance(data, dict) or set(data) - set(FORMATS):
            raise ValueError('Invalid Home format usage owner')
        for format, number in data.items():
            _number(number)
            if number is None or number < 0 or field != 'scores' and type(number) is not int:
                raise ValueError('Invalid Home format usage support')
            rows.append((format, field, number))
    for field in ('vibe_episodes', 'half_life_days'):
        _number(usage.get(field))
    metadata = (_number(at), _number(value.get('at', value.get('built_at'))), schema,
                _identity(value.get('watermark')), _identity(value.get('report_id')),
                _identity(refresh.get('generation')), _identity(refresh.get('inputs', value.get('inputs'))),
                _number(refresh.get('next_at')), _number(usage.get('vibe_episodes')),
                _number(usage.get('half_life_days')), hashlib.sha256(raw.encode()).hexdigest(),
                len(raw.encode()), int(bool(value)))
    parts = tuple(files.encode(surface, str(position), raw[start:start + PART_CHARACTERS])
                  for position, start in enumerate(range(0, len(raw), PART_CHARACTERS)))
    return Prepared(surface, raw, parts, metadata, tuple(rows))


def publish(conn, prepared):
    if not conn.in_transaction or not isinstance(prepared, Prepared):
        raise ValueError('Report publication requires prepared output and an outer transaction')
    durable = [(prepared.surface, str(position), *files.publish(files.directory(conn), encoded))
               for position, encoded in enumerate(prepared.parts)]
    return register(conn, prepared, durable)


def register(conn, prepared, durable):
    """Install references to already durable report parts."""
    if not conn.in_transaction or len(durable) != len(prepared.parts):
        raise ValueError('Report publication requires all prepared parts and an outer transaction')
    identity = publication_files.next_identity(conn, PREFIX)
    parts = []
    for position, (surface, key, filename, digest, size) in enumerate(durable):
        if surface != prepared.surface or key != str(position):
            raise ValueError('Report part belongs to a different publication or position')
        parts.append((identity * 64 + position, filename, digest, size))
    publication_files.save(conn, PREFIX + prepared.surface, {
        **dict(zip(METADATA, prepared.metadata, strict=True)), 'id': identity, 'surface': prepared.surface,
        'part_count': len(parts), 'parts': parts, 'format_usage': prepared.usage,
    })
    return identity


def manifest(conn, surface):
    """Capture the selected report's SQL references without opening files."""
    row = publication_files.fields(conn, PREFIX + surface, 'content_digest', 'total_bytes', 'part_count', 'parts')
    if row is None:
        return None
    digest, size, count, raw = row
    parts = [dict(chunk_key=str(position), filename=part[1], content_digest=part[2], byte_size=part[3])
             for position, part in enumerate(json.loads(raw)) if part is not None]
    return {'content_digest': digest, 'total_bytes': size, 'part_count': count, 'parts': parts}


def checkpoint(conn, surface):
    row = publication_files.fields(conn, PREFIX + surface, 'observed_at', 'schema_version', 'watermark',
                                   'report_key', 'evidence_generation', 'inputs_key', 'next_at')
    return dict(zip(('at', 'schema', 'watermark', 'report_id', 'generation', 'inputs', 'next_at'), row)) if row else None


def present(conn, surface):
    row = publication_files.fields(conn, PREFIX + surface, 'has_content')
    return bool(row and row[0])


def choice_usage(conn):
    row = publication_files.fields(conn, PREFIX + 'listening_choices', 'format_usage', 'vibe_episodes', 'half_life_days')
    if row is None:
        return {}
    result = {'scores': {}, 'episodes': {}, 'days': {}}
    for format, field, number in json.loads(row[0]):
        result[field][format] = number
    for field, number in zip(('vibe_episodes', 'half_life_days'), row[1:]):
        if number is not None:
            result[field] = number
    return result


def clear(conn, surfaces=SURFACES):
    if not conn.in_transaction:
        raise ValueError('Report invalidation requires the outer transaction')
    for surface in surfaces:
        publication_files.clear(conn, PREFIX + surface)
