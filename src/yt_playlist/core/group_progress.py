"""Bounded progress owner extending the frozen v94 grouping phase domain.

The historical table still owns its fit/generation checkpoint. Its original
progress fields remain retained after this independently recorded replacement.
"""
import json
import math

from .schema_contract import MigrationError

TABLE = 'group_preparation_progress'
PHASES = ('preparing', 'ready', 'deferred', 'failed')
SQL = f'''CREATE TABLE {TABLE} (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    phase TEXT CHECK(phase IN ('preparing','ready','deferred','failed')),
    reported_at REAL CHECK(reported_at IS NULL OR
        (typeof(reported_at) IN ('integer','real') AND abs(reported_at)<=1.7976931348623157e308)),
    reason TEXT,
    CHECK((phase IS NULL AND reported_at IS NULL AND reason IS NULL) OR
          (phase IS NOT NULL AND reported_at IS NOT NULL))
)'''


def available(conn):
    return conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (TABLE,)).fetchone() is not None


def definitions(conn):
    yield TABLE, SQL
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone():
        return
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        name = 'group_progress_legacy_' + event.lower()
        operation = 'UPDATE OF phase,reported_at,reason' if event == 'UPDATE' else event
        body = (f'DELETE FROM {TABLE} WHERE singleton=OLD.singleton;' if event == 'DELETE' else
                f'INSERT INTO {TABLE} VALUES (NEW.singleton,NEW.phase,NEW.reported_at,NEW.reason) '
                'ON CONFLICT(singleton) DO UPDATE SET phase=excluded.phase,'
                'reported_at=excluded.reported_at,reason=excluded.reason;')
        yield name, f'CREATE TRIGGER {name} AFTER {operation} ON main.group_preparation_state BEGIN {body} END'


def validate(conn):
    from .schema_contract import tokens as _tokens
    for name, sql in definitions(conn):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible grouping progress owner: ' + name)


def install(conn):
    from .schema_contract import tokens as _tokens
    added = not available(conn)
    for name, sql in definitions(conn):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None:
            conn.execute(sql)
        elif _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible grouping progress owner: ' + name)
    if added and conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone():
        conn.execute(f'INSERT INTO {TABLE} SELECT singleton,phase,reported_at,reason '
                     'FROM group_preparation_state')


def read(conn, default=None):
    row = conn.execute(f'SELECT phase,reported_at,reason FROM {TABLE} WHERE singleton=1').fetchone()
    return json.dumps({'phase': row[0], 'at': row[1], 'reason': row[2]}) if row and row[0] else default


def write(conn, value):
    if not isinstance(value, dict) or set(value) - {'phase', 'at', 'reason'}:
        raise ValueError('Unsupported grouping checkpoint; original retained')
    phase, at, reason = (value.get(key) for key in ('phase', 'at', 'reason'))
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('Unsupported model checkpoint group time; original retained')
    if reason is not None and not isinstance(reason, str):
        raise ValueError('Unsupported model checkpoint group reason; original retained')
    if not ((phase is None and at is None and reason is None)
            or (isinstance(phase, str) and phase in PHASES and at is not None)):
        raise ValueError('Grouping progress requires its phase and recorded time; original retained')
    conn.execute(f'INSERT INTO {TABLE} VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET '
                 'phase=excluded.phase,reported_at=excluded.reported_at,reason=excluded.reason',
                 (phase, at, reason))
