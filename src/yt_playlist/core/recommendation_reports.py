"""Fixed report publications, with serving decisions held in typed relations.

The files contain bounded diagnostic output. Home format selection and refresh
admission read SQL fields and never depend on decoding a diagnostic document.
"""
from dataclasses import dataclass
import hashlib
import math

from . import recommendation_outputs as files

SURFACES = frozenset({'listening_choices', 'choice_daily', 'model_health', 'model_live_report'})
FORMATS = ('mixes', 'albums', 'radio')
MAX_BYTES = 4 * 1024 * 1024
PART_CHARACTERS = 64 * 1024


def prepared_owner(conn):
    from .report_reads import active
    return active(conn)


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
    previous = conn.execute('SELECT publication_id FROM recommendation_report_heads WHERE surface=?',
                            (prepared.surface,)).fetchone()
    identity = conn.execute('INSERT INTO recommendation_report_publications('
        'surface,built_at,observed_at,schema_version,watermark,report_key,evidence_generation,inputs_key,next_at,'
        'vibe_episodes,half_life_days,content_digest,total_bytes,has_content,part_count) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
        (prepared.surface, *prepared.metadata, len(durable))).fetchone()[0]
    conn.executemany('INSERT INTO recommendation_report_files('
        'publication_id,surface,chunk_key,filename,content_digest,byte_size) VALUES (?,?,?,?,?,?)',
        [(identity, *row) for row in durable])
    conn.executemany('INSERT INTO choice_format_usage VALUES (?,?,?,?)',
                     [(identity, *row) for row in prepared.usage])
    conn.execute("UPDATE recommendation_report_publications SET status='published' WHERE id=?", (identity,))
    conn.execute('INSERT INTO recommendation_report_heads VALUES (?,?) ON CONFLICT(surface) '
                 'DO UPDATE SET publication_id=excluded.publication_id', (prepared.surface, identity))
    if previous:
        _retire(conn, previous[0])
    return identity


def manifest(conn, surface):
    """Capture the selected report's SQL references without opening files."""
    prepared = prepared_owner(conn)
    if prepared:
        from .report_reads import selected
        row = selected(conn, surface)
        if row is None:
            return None
        parts = conn.execute('SELECT CAST(position AS TEXT) AS chunk_key,filename,'
            'digest AS content_digest,byte_size FROM prepared_report_parts '
            'WHERE generation=? AND surface=? ORDER BY position LIMIT 65',
            (row['generation'], surface)).fetchall()
        metadata = (row['content_digest'], row['total_bytes'], row['part_count'])
    else:
        row = conn.execute('SELECT p.id,p.content_digest,p.total_bytes,p.part_count '
            'FROM recommendation_report_heads h JOIN recommendation_report_publications p '
            'ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()
        if row is None:
            return None
        parts = conn.execute('SELECT chunk_key,filename,content_digest,byte_size '
            'FROM recommendation_report_files WHERE publication_id=? ORDER BY CAST(chunk_key AS INTEGER)',
            (row[0],)).fetchall()
        metadata = tuple(row[1:])
    return {'content_digest': metadata[0], 'total_bytes': metadata[1], 'part_count': metadata[2],
            'prepared': prepared, 'parts': [dict(part) for part in parts]}


def _retire(conn, identity):
    conn.execute("UPDATE recommendation_report_publications SET status='retiring' WHERE id=?", (identity,))
    conn.execute('DELETE FROM choice_format_usage WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM recommendation_report_files WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM recommendation_report_publications WHERE id=?', (identity,))


def encoded(conn, surface):
    if prepared_owner(conn):
        from .schema_contract import MigrationError
        from . import report_reads
        row = report_reads.selected(conn, surface)
        try:
            return report_reads.read_original(conn, row) if row is not None else None
        except (OSError, ValueError, MigrationError):
            return None
    row = conn.execute('SELECT p.id,p.content_digest,p.total_bytes,p.part_count '
        'FROM recommendation_report_heads h JOIN recommendation_report_publications p '
        'ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()
    if row is None:
        return None
    parts = conn.execute('SELECT chunk_key,filename,content_digest,byte_size FROM recommendation_report_files '
        'WHERE publication_id=? ORDER BY CAST(chunk_key AS INTEGER)', (row[0],)).fetchall()
    if len(parts) != row[3] or [part[0] for part in parts] != list(map(str, range(row[3]))):
        raise ValueError('Incomplete recommendation report')
    try:
        raw = ''.join(files.read(*part[1:], surface=surface, chunk_key=part[0]) for part in parts)
    except (OSError, ValueError):
        return None
    if len(raw.encode()) != row[2] or hashlib.sha256(raw.encode()).hexdigest() != row[1]:
        raise ValueError('Report differs from its publication digest')
    return raw


def checkpoint(conn, surface):
    if prepared_owner(conn):
        from .report_reads import selected
        row = selected(conn, surface)
        return dict(zip(('at', 'schema', 'watermark', 'report_id', 'generation', 'inputs', 'next_at'),
            (row[name] for name in ('observed_at', 'schema_version', 'watermark', 'report_key',
                                  'evidence_generation', 'inputs_key', 'next_at')))) if row else None
    row = conn.execute('SELECT p.observed_at,p.schema_version,p.watermark,p.report_key,'
        'p.evidence_generation,p.inputs_key,p.next_at FROM recommendation_report_heads h '
        'JOIN recommendation_report_publications p ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()
    return dict(zip(('at', 'schema', 'watermark', 'report_id', 'generation', 'inputs', 'next_at'), row)) if row else None


def present(conn, surface):
    if prepared_owner(conn):
        from .report_reads import selected
        row = selected(conn, surface)
        return row is not None and row['has_content'] == 1
    return conn.execute('SELECT 1 FROM recommendation_report_heads h JOIN recommendation_report_publications p '
        'ON p.id=h.publication_id WHERE h.surface=? AND p.has_content=1', (surface,)).fetchone() is not None


def choice_usage(conn):
    if prepared_owner(conn):
        from .report_reads import selected
        row = selected(conn, 'listening_choices')
        if row is None:
            return {}
        result = {'scores': {}, 'episodes': {}, 'days': {}}
        for format, field, number in conn.execute('SELECT format,field,value FROM prepared_report_usage '
                'WHERE generation=? AND surface=?', (row['generation'], row['surface'])):
            result[field][format] = number
        for field in ('vibe_episodes', 'half_life_days'):
            if row[field] is not None:
                result[field] = row[field]
        return result
    row = conn.execute('SELECT p.id,p.vibe_episodes,p.half_life_days FROM recommendation_report_heads h '
        "JOIN recommendation_report_publications p ON p.id=h.publication_id WHERE h.surface='listening_choices'").fetchone()
    if row is None:
        return {}
    result = {'scores': {}, 'episodes': {}, 'days': {}}
    for format, field, number in conn.execute('SELECT format,field,value FROM choice_format_usage WHERE publication_id=?', (row[0],)):
        result[field][format] = number
    for field, number in zip(('vibe_episodes', 'half_life_days'), row[1:]):
        if number is not None:
            result[field] = number
    return result


def clear(conn, surfaces=SURFACES):
    if not conn.in_transaction:
        raise ValueError('Report invalidation requires the outer transaction')
    for surface in surfaces:
        row = conn.execute('SELECT publication_id FROM recommendation_report_heads WHERE surface=?', (surface,)).fetchone()
        if row:
            conn.execute('DELETE FROM recommendation_report_heads WHERE surface=?', (surface,))
            _retire(conn, row[0])
