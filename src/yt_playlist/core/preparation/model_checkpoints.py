"""Prepare finite content/group/candidate checkpoints without running workers.

These independent jobs retain installed settings and exact observed revisions.
They do not fit models, admit generations, prepare vibes or select runtime owners.
"""
import json
import math
from dataclasses import replace

from .. import content_preparation_state as content, group_progress, recommendation_outputs
from . import collection_controls
from .rows import main as run


MAX_BYTES = collection_controls.MAX_BYTES
CONTENT = tuple(content.KEYS)
GROUP = ('rec_group_shadow_progress', 'rec_group_shadow_checkpoint', 'rec_mode_fit_inputs')
CANDIDATES = ('rec_candidate_preferences', 'rec_candidates_expire_at')
TABLES = {
    **{key: 'prepared_' + table for key, table in content.KEYS.items()},
    'rec_group_shadow_progress': 'prepared_group_progress_checkpoint',
    'rec_group_shadow_checkpoint': 'prepared_group_generation_checkpoint',
    'rec_mode_fit_inputs': 'prepared_group_fit_checkpoint',
    'rec_candidate_preferences': 'prepared_candidate_preferences_checkpoint',
    'rec_candidates_expire_at': 'prepared_candidate_expiry_checkpoint',
}
PROGRESS_V2 = 'prepared_group_progress_checkpoint_v2'
LEGACY_PROGRESS = TABLES[GROUP[0]]
TABLES[GROUP[0]] = PROGRESS_V2


def integer(value, minimum=0):
    return type(value) is int and minimum <= value < 2**63


def number(value):
    return collection_controls.finite(value)


def text(value):
    return isinstance(value, str)


INT = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0}>=0))"
REAL = "REAL CHECK({0} IS NULL OR (typeof({0}) IN ('integer','real') AND abs({0})<=1.7976931348623157e308))"
TEXT = 'TEXT'
FIELDS = {
    'serial': (INT, integer), 'rows': (INT, integer), 'at': (REAL, number),
    'reason': (TEXT, text), 'estimated_bytes': (INT, integer), 'dimensions': (INT, integer),
    'input_fingerprint': (TEXT, text), 'content': (INT, integer), 'evidence': (INT, integer),
    'state': ("TEXT CHECK(state IS NULL OR state IN ('ready','waiting','deferred'))",
              lambda value: isinstance(value, str) and value in ('ready', 'waiting', 'deferred')),
    'attempted_at': (REAL, number), 'attempts': (INT, integer), 'since': (REAL, number),
    'next_attempt_at': (REAL, number), 'memory_bytes': (INT, integer), 'max_rows': (INT, integer),
}
FLAG = "INTEGER CHECK({0} IN (0,1))"
POSITIVE = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0}>0))"
FRACTION = "REAL CHECK({0} IS NULL OR (typeof({0}) IN ('integer','real') AND {0} BETWEEN 0 AND 1))"


def columns(key, *, legacy=False):
    if key in CONTENT:
        fields = {name: FIELDS[name][0] for name in content.FIELDS[key]}
        if key == 'rec_preparation_full':
            fields.update(has_config=FLAG, **dict.fromkeys(content.CONFIG, TEXT))
        else:
            fields.update(has_phases=FLAG, **dict.fromkeys(('bytes_' + p for p in content.PHASES), INT))
        if key == 'rec_preparation_basis':
            fields.update(has_modes=FLAG, mode_count=POSITIVE, mode_min_members=POSITIVE,
                          mode_match_threshold=FRACTION)
        return fields
    return {
        GROUP[0]: {'phase': ("TEXT CHECK(phase IS NULL OR phase IN ('preparing','ready','deferred'))" if legacy else
                            "TEXT CHECK(phase IS NULL OR phase IN ('preparing','ready','deferred','failed'))"),
                   'reported_at': REAL, 'reason': TEXT},
        GROUP[1]: {'recorded_generation': TEXT, 'generation_id': POSITIVE,
                   'unresolved_reason': "TEXT CHECK(unresolved_reason IS NULL OR unresolved_reason='missing_generation')",
                   'input_serial': INT, 'checkpoint_at': REAL},
        GROUP[2]: {'fit_fingerprint': TEXT},
        CANDIDATES[0]: {'preferences_fingerprint': TEXT},
        CANDIDATES[1]: {'expires_at': REAL},
    }[key]


def checked(value, predicate, description):
    if value is not None and not predicate(value):
        raise ValueError('Unsupported model checkpoint ' + description + '; original retained')
    return value


def content_values(key, value):
    allowed = set(content.FIELDS[key]) | ({'config'} if key == 'rec_preparation_full' else {'phase_bytes'})
    if key == 'rec_preparation_basis':
        allowed.add('modes')
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('Unsupported content preparation checkpoint; original retained')
    result = {name: checked(value.get(name), FIELDS[name][1], name) for name in content.FIELDS[key]}
    if key == 'rec_preparation_full':
        config = value.get('config')
        if config is not None and (not isinstance(config, list) or len(config) != len(content.CONFIG)
                                  or any(item is not None and not isinstance(item, str) for item in config)):
            raise ValueError('Content configuration requires seven scalar settings; original retained')
        result.update(has_config=int(config is not None), **dict(zip(content.CONFIG, config or [None] * len(content.CONFIG))))
    else:
        phases = value.get('phase_bytes')
        if phases is not None and (not isinstance(phases, dict) or set(phases) - set(content.PHASES)):
            raise ValueError('Unsupported content memory phase; original retained')
        result['has_phases'] = int(phases is not None)
        result.update(('bytes_' + p, checked((phases or {}).get(p), integer, 'phase bytes')) for p in content.PHASES)
    if key == 'rec_preparation_basis':
        modes = value.get('modes')
        if modes is not None and (not isinstance(modes, list) or len(modes) != 3
                or not integer(modes[0], 1) or not integer(modes[1], 1)
                or not number(modes[2]) or not 0 <= modes[2] <= 1):
            raise ValueError('Content checkpoint requires three valid mode parameters; original retained')
        result.update(has_modes=int(modes is not None), **dict(zip(content.MODES, modes or [None] * 3)))
    return result


def group_values(conn, key, value):
    allowed = {'phase', 'at', 'reason'} if key == GROUP[0] else {'generation', 'serial', 'at'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('Unsupported grouping checkpoint; original retained')
    at = checked(value.get('at'), number, 'group time')
    if key == GROUP[0]:
        phase = value.get('phase')
        reason = checked(value.get('reason'), text, 'group reason')
        if not ((phase is None and at is None and reason is None)
                or (isinstance(phase, str) and phase in group_progress.PHASES and at is not None)):
            raise ValueError('Grouping progress requires its phase and recorded time; original retained')
        return {'phase': phase, 'reported_at': at, 'reason': reason}
    generation = checked(value.get('generation'), text, 'generation')
    serial = checked(value.get('serial'), integer, 'group serial')
    if not ((generation is None and serial is None and at is None)
            or (generation is not None and serial is not None and at is not None)):
        raise ValueError('Grouping checkpoint requires its exact generation, serial and time; original retained')
    row = conn.execute('SELECT id FROM main.group_generations WHERE external_key=?', (generation,)).fetchone()
    return {'recorded_generation': generation, 'generation_id': row[0] if row else None,
            'unresolved_reason': 'missing_generation' if generation is not None and row is None else None,
            'input_serial': serial, 'checkpoint_at': at}


def values(conn, key, raw):
    result = dict.fromkeys(columns(key))
    kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
    if kind in ('null', 'blank'):
        return kind, result
    if key in CONTENT or key in GROUP[:2]:
        value = recommendation_outputs.decode_json(raw)
        result = content_values(key, value) if key in CONTENT else group_values(conn, key, value)
    elif key == GROUP[2]:
        result['fit_fingerprint'] = raw
    elif key == CANDIDATES[0]:
        result['preferences_fingerprint'] = raw
    else:
        try:
            expiry = float(raw)
        except (ValueError, OverflowError) as exc:
            raise ValueError('Candidate expiry requires a finite recorded time; original retained') from exc
        if not math.isfinite(expiry):
            raise ValueError('Candidate expiry requires a finite recorded time; original retained')
        result['expires_at'] = expiry
    return kind, result


def copy(conn, owner, source_key, row):
    key = row['key']
    name = next(name for name, keys in SOURCES.items() if key in keys)
    raw, digest = collection_controls.retain(conn, name, row)
    kind, fields = values(conn, key, raw)
    names = ('source', 'source_kind', *fields, 'source_digest')
    assignments = ','.join(column + '=excluded.' + column for column in names[1:])
    conn.execute(f'INSERT INTO {table(conn, key)} ({",".join(names)}) '
                 f'VALUES ({",".join("?" for _ in names)}) ON CONFLICT(source) DO UPDATE SET {assignments}',
                 (key, kind, *fields.values(), digest))


def remove(conn, owner, source_key):
    key, = json.loads(source_key)
    if key in TABLES:
        conn.execute(f'DELETE FROM {table(conn, key)} WHERE source=?', (key,))


def table(conn, key):
    if key == GROUP[0] and not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (PROGRESS_V2,)).fetchone():
        return LEGACY_PROGRESS
    return TABLES[key]


SOURCES = {'content_checkpoints': CONTENT, 'group_checkpoints': GROUP, 'candidate_checkpoints': CANDIDATES}


def definitions(name, keys):
    allowed = ','.join(repr(key) for key in keys)
    archive = collection_controls.originals(name)
    yield archive, f'''CREATE TABLE {archive} (
        source TEXT NOT NULL CHECK(source IN ({allowed})), digest TEXT NOT NULL CHECK(length(digest)=64),
        raw_value TEXT CHECK(raw_value IS NULL OR length(CAST(raw_value AS BLOB))<={MAX_BYTES}),
        PRIMARY KEY(source,digest)) WITHOUT ROWID'''
    for key in keys:
        table = LEGACY_PROGRESS if key == GROUP[0] else TABLES[key]
        yield table, definition(name, key, table, legacy=True)


def definition(name, key, table, *, legacy=False):
    archive = collection_controls.originals(name)
    fields = ','.join('"' + column + '" ' + sql.format(column) for column, sql in columns(key, legacy=legacy).items())
    return f'''CREATE TABLE {table} (
        source TEXT PRIMARY KEY CHECK(source={key!r}), source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        {fields}, source_digest TEXT NOT NULL,
        FOREIGN KEY(source,source_digest) REFERENCES {archive}(source,digest)) WITHOUT ROWID'''


JOBS = {name.replace('_', '-'): collection_controls.job(name, keys, tuple(definitions(name, keys)), copy, remove,
        ('group_generations',) if name == 'group_checkpoints' else ()) for name, keys in SOURCES.items()}
JOBS['group-checkpoints'] = replace(JOBS['group-checkpoints'], additive_destinations=((PROGRESS_V2,
    definition('group_checkpoints', GROUP[0], PROGRESS_V2)),))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
