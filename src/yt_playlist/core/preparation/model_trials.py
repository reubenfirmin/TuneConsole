"""Prepare retained trial decisions, monitoring and held assignments independently.

The frozen v95 field contract supplies destination constraints. Exact source
revisions and ordered baselines remain additive. Installed proposals stay
authoritative; this job neither admits runs nor publishes a runtime owner.
"""
from dataclasses import dataclass
import hashlib
import json
import math

from .. import model_comparison_state as state
from ..migrations import current as schema
from ..migrations.runner import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 256 * 1024
MAX_BASELINE = 4096
SURFACES = {'live-trial': 'model_live_comparison', 'listening-monitor': 'model_listening_monitor',
            'album-assignment': 'model_album_assignment'}
PREFIXES = {'model_live_comparison': 'prepared_trial_plan', 'model_listening_monitor': 'prepared_trial_monitor',
            'model_album_assignment': 'prepared_trial_assignment'}
BASELINE = 'prepared_trial_monitor_baseline'


def tables(surface):
    prefix = PREFIXES[surface]
    return prefix + '_generations', prefix + '_heads', prefix + '_originals'


def definitions(surface):
    head, current, originals = tables(surface)
    yield originals, f'''CREATE TABLE {originals} (
        source TEXT NOT NULL CHECK(source={surface!r}), digest TEXT NOT NULL CHECK(length(digest)=64),
        raw_value TEXT NOT NULL CHECK(length(CAST(raw_value AS BLOB)) BETWEEN 1 AND {MAX_BYTES}),
        built_at REAL CHECK(built_at IS NULL OR abs(built_at)<=1.7976931348623157e308),
        PRIMARY KEY(source,digest)) WITHOUT ROWID'''
    native = state.OWNERS[surface][0]
    sql, = (sql for sql in schema.statements(version=95) if sql.startswith('CREATE TABLE ' + native + ' ('))
    # Preserve the finite historical checks, replacing only its singleton key
    # and eager run FK. Source-run edits must remain usable while preparation
    # is incomplete; their journal reconciles the explicit run resolution.
    body = sql.removeprefix('CREATE TABLE ' + native + ' (')
    singleton = 'id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),'
    if not body.startswith(singleton) or not body.endswith(')'):
        raise MigrationError('Unexpected frozen model control definition')
    body = body.removeprefix(singleton)[:-1].replace('run_ref INTEGER REFERENCES rec_model_selection_runs(id)', 'run_ref INTEGER')
    yield head, (f'CREATE TABLE {head} (source TEXT NOT NULL CHECK(source={surface!r}), '
        'preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0), source_digest TEXT NOT NULL, ' + body +
        f', PRIMARY KEY(source,preparation_revision), FOREIGN KEY(source,source_digest) REFERENCES {originals}(source,digest)) WITHOUT ROWID')
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY CHECK(source={surface!r}), preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    if surface == 'model_listening_monitor':
        yield BASELINE, f'''CREATE TABLE {BASELINE} (
            source TEXT NOT NULL, preparation_revision INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_BASELINE - 1}),
            value REAL NOT NULL CHECK(typeof(value) IN ('integer','real') AND value BETWEEN 0 AND 1),
            PRIMARY KEY(source,preparation_revision,position), FOREIGN KEY(source,preparation_revision)
                REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''


@dataclass(frozen=True)
class Plan:
    raw: str
    digest: str
    fields: dict
    units: tuple


def prepare(conn, row):
    raw = bytes(row['payload'])
    if len(raw) > MAX_BYTES:
        raise MigrationError('Model trial exceeds its preparation byte budget')
    at = row['built_at']
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('Model trial requires a finite captured observation time')
    text = raw.decode('utf-8')
    value = state.decode(row['surface'], text)
    digest = hashlib.sha256(json.dumps([text, at], ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    fields = state.parse(row['surface'], value)
    baseline = tuple(value.get('baseline') or ()) if row['surface'] == 'model_listening_monitor' else ()
    return Plan(text, digest, fields, baseline)


def begin(conn, row, generation, plan):
    head, _current, originals = tables(row['surface'])
    conn.execute(f'INSERT INTO {originals} VALUES (?,?,?,?) ON CONFLICT(source,digest) DO NOTHING',
                 (row['surface'], plan.digest, plan.raw, row['built_at']))
    fields = dict(plan.fields)
    column = 'comparison_run_id' if row['surface'] == 'model_album_assignment' else 'run_id'
    raw = fields[column]
    reference = state._run(conn, raw)
    fields.update(run_ref=reference, run_resolution=None if raw is None or reference is not None else 'missing_run',
                  built_at=row['built_at'])
    columns = ','.join('"' + name + '"' for name in fields)
    conn.execute(f'INSERT INTO {head}(source,preparation_revision,source_digest,{columns}) '
                 f'VALUES (?,?,?,{",".join("?" for _ in fields)})',
                 (row['surface'], generation, plan.digest, *fields.values()))


def copy(conn, row, generation, position, value):
    conn.execute(f'INSERT INTO {BASELINE} VALUES (?,?,?,?)', (row['surface'], generation, position, value))


def complete(conn, row, generation, plan):
    _head, current, _originals = tables(row['surface'])
    conn.execute(f'INSERT INTO {current} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET preparation_revision=excluded.preparation_revision',
                 (row['surface'], generation))


def delete(conn, owner, key):
    surface, = json.loads(key)
    if surface in PREFIXES:
        conn.execute('DELETE FROM ' + tables(surface)[1] + ' WHERE source=?', (surface,))


def definition(surface):
    return Job('trial_' + surface.removeprefix('model_'), 79, 94, {'rec_proposals': ('surface',)},
        tuple(definitions(surface)), None, None, dependencies=('rec_model_selection_runs',),
        filters={'rec_proposals': '{prefix}surface=' + repr(surface)}, projections={'rec_proposals': (
            'surface', f"COALESCE(substr(CAST(payload AS BLOB),1,{MAX_BYTES + 1}),X'') AS payload", 'built_at')})


JOBS = {name: OrderedJob(definition(surface), prepare, begin, copy, complete, delete) for name, surface in SURFACES.items()}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
