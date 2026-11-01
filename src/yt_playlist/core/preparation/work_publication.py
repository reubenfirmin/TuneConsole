"""Publish verified work checkpoints without retiring their installed documents.

The two documents have fixed schemas. After publication every source mutation
validates and updates the typed runtime owner in the same SQLite transaction.
Existing processes and newly opened readers therefore observe one boundary.
The retained documents continue to support the frozen historical upgrade route.
"""
import json
import sqlite3
import time

from ..migrations.runner import MigrationError
from ..recommendation_work import parse
from .recommendation_state import BYTE_LIMITS, JOBS, document, projection


HEAD = 'published_work'
TABLES = {'daily_model_schedule': 'prepared_model_selection_schedule',
          'tv_preparation': 'prepared_tv_preparation_state'}
JOB = JOBS['work']
LIMIT = '1.7976931348623157e308'


def _number(path):
    return (f"json_type(NEW.payload,'{path}') IN ('integer','real') AND "
            f"json_extract(NEW.payload,'{path}') BETWEEN -{LIMIT} AND {LIMIT}")


def definitions():
    yield HEAD, f'''CREATE TABLE {HEAD} (
        id INTEGER PRIMARY KEY CHECK(id=1), prepared_revision INTEGER NOT NULL,
        published_at REAL NOT NULL, write_revision INTEGER NOT NULL DEFAULT 0 CHECK(write_revision>=0))'''
    yield 'published_work_originals', '''CREATE TABLE published_work_originals (
        surface TEXT PRIMARY KEY CHECK(surface IN ('daily_model_schedule','tv_preparation')),
        payload TEXT NOT NULL, built_at REAL NOT NULL) WITHOUT ROWID'''
    for operation in ('UPDATE', 'DELETE'):
        name = 'published_work_originals_' + operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.published_work_originals '
                     "BEGIN SELECT RAISE(ABORT,'Published work originals require the future cleanup procedure'); END")
    for operation in ('INSERT', 'UPDATE'):
        name = 'published_work_validate_' + operation.lower()
        finite_time = f"typeof(NEW.built_at) IN ('integer','real') AND NEW.built_at BETWEEN -{LIMIT} AND {LIMIT}"
        daily = ("NOT EXISTS(SELECT 1 FROM json_each(NEW.payload) WHERE key<>'next_at') AND "
                 f"(NOT EXISTS(SELECT 1 FROM json_each(NEW.payload)) OR ({_number('$.next_at')}))")
        tv = ("NOT EXISTS(SELECT 1 FROM json_each(NEW.payload) WHERE key NOT IN ('failed','at')) AND "
              "(NOT EXISTS(SELECT 1 FROM json_each(NEW.payload)) OR "
              "(json_type(NEW.payload,'$.failed') IN ('true','false') AND "
              f"(json_type(NEW.payload,'$.at') IS NULL OR ({_number('$.at')}))))")
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.rec_proposals '
            "WHEN NEW.surface IN ('daily_model_schedule','tv_preparation') BEGIN "
            f"SELECT CASE WHEN length(CAST(NEW.payload AS BLOB))>{BYTE_LIMITS['work']} "
            "THEN RAISE(ABORT,'Published work document exceeds its byte budget') END; "
            "SELECT CASE WHEN (json_valid(NEW.payload) AND json_type(NEW.payload)='object' AND "
            '(SELECT count(*)=count(DISTINCT key) FROM json_each(NEW.payload)) AND '
            f"({finite_time}) AND (CASE NEW.surface WHEN 'daily_model_schedule' THEN ({daily}) ELSE ({tv}) END)) "
            "IS NOT TRUE THEN RAISE(ABORT,'Invalid published recommendation work state') END; END")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = 'published_work_sync_' + operation.lower()
        body = ''
        if operation != 'INSERT':
            for surface, table in TABLES.items():
                body += f"DELETE FROM {table} WHERE OLD.surface='{surface}';"
        if operation != 'DELETE':
            for surface, table in TABLES.items():
                body += f"DELETE FROM {table} WHERE NEW.surface='{surface}';"
            payload = "CASE WHEN NEW.surface IN ('daily_model_schedule','tv_preparation') THEN NEW.payload ELSE '{}' END"
            body += (f"INSERT INTO prepared_model_selection_schedule SELECT 1,json_extract({payload},'$.next_at'),NEW.built_at "
                     f"WHERE NEW.surface='daily_model_schedule' AND EXISTS(SELECT 1 FROM json_each({payload}));"
                     f"INSERT INTO prepared_tv_preparation_state SELECT 1,json_extract({payload},'$.failed'),"
                     f"json_extract({payload},'$.at'),NEW.built_at WHERE NEW.surface='tv_preparation' "
                     f"AND EXISTS(SELECT 1 FROM json_each({payload}));")
        sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
        condition = ' OR '.join(f"{side}.surface IN ('daily_model_schedule','tv_preparation')" for side in sides)
        yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.rec_proposals WHEN {condition} BEGIN ' +
                     body + f'UPDATE {HEAD} SET write_revision=write_revision+1 WHERE id=1; END')


def validate(conn):
    from ..migrations.current import _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible work publication object: ' + name)


def validate_installed(conn):
    """Validate active publication definitions without scanning installed data."""
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='model_selection_schedule'").fetchone():
        return
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    JOB.validate(conn)
    validate(conn)
    if conn.execute(f'SELECT 1 FROM {HEAD} WHERE id=1').fetchone() is None:
        raise MigrationError('Active work publication has no committed head')


def publish(conn, *, revision, clock=time.time):
    """Verify two fixed documents and atomically admit their typed runtime owner."""
    if conn.in_transaction:
        raise MigrationError('Publication requires an idle connection')
    try:
        conn.execute('BEGIN IMMEDIATE')
        JOB._version(conn)
        JOB.validate(conn)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
            if conn.execute(f'SELECT 1 FROM {HEAD} WHERE id=1').fetchone():
                conn.rollback()
                return
            raise MigrationError('Incomplete work publication has no committed head')
        progress = JOB.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.prepared_revision != revision
                or progress.input_revision != revision or conn.execute(f'SELECT 1 FROM {JOB.changes} LIMIT 1').fetchone()):
            raise MigrationError('Work publication requires the requested reconciled preparation revision')
        columns = ','.join(projection('work'))
        originals = {r['surface']: (document('work', r)['payload'], r['built_at']) for r in conn.execute(
            f"SELECT {columns} FROM main.rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation')")}
        retained = {r['surface']: (document('work', r)['payload'], r['built_at']) for r in conn.execute(
            f'SELECT {columns} FROM prepared_work_originals')}
        if originals != retained:
            raise MigrationError('Prepared work originals differ from the installed documents')
        for surface, table in TABLES.items():
            expected = parse(surface, json.loads(originals[surface][0]), originals[surface][1]) if surface in originals else None
            row = conn.execute(f'SELECT * FROM {table}').fetchall()
            actual = tuple(row[0])[1:] if len(row) == 1 else None
            if len(row) > 1 or actual != expected:
                raise MigrationError('Prepared work owner differs from its verified input: ' + surface)
        for _, sql in definitions():
            conn.execute(sql)
        conn.execute('INSERT INTO published_work_originals SELECT * FROM prepared_work_originals')
        conn.execute(f'INSERT INTO {HEAD}(id,prepared_revision,published_at) VALUES (1,?,?)', (revision, clock()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--revision', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            publish(conn, revision=args.revision)
            print(json.dumps(dict(conn.execute(f'SELECT * FROM {HEAD} WHERE id=1').fetchone())))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'Work publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
