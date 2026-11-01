"""Select verified report files without retiring their continuing source writers.

Source triggers invalidate the selection in the same transaction as any report
edit. Readers then use the installed document until explicit preparation and
publication select a new revision. Old file generations remain retained.
"""
import hashlib
import json
import sqlite3
import time

from .. import recommendation_outputs as files, recommendation_reports as reports
from ..migrations.runner import MigrationError
from . import recommendation_reports as preparation


HEAD = 'published_reports'
LINKS = 'published_report_heads'
JOB = preparation.DEFINITION


def definitions():
    yield HEAD, f'''CREATE TABLE {HEAD} (
        id INTEGER PRIMARY KEY CHECK(id=1), prepared_revision INTEGER NOT NULL CHECK(prepared_revision>=0),
        published_at REAL NOT NULL)'''
    yield LINKS, f'''CREATE TABLE {LINKS} (
        revision INTEGER NOT NULL CHECK(revision>=0), surface TEXT NOT NULL,
        generation INTEGER NOT NULL,
        PRIMARY KEY(revision,surface),
        FOREIGN KEY(generation,surface) REFERENCES {preparation.HEAD}(generation,surface)) WITHOUT ROWID'''
    for operation in ('UPDATE', 'DELETE'):
        name = 'published_report_heads_' + operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{LINKS} '
                     "BEGIN SELECT RAISE(ABORT,'Published report links are retained for cleanup'); END")
    for table in (preparation.HEAD, preparation.PARTS, 'prepared_report_usage'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
            condition = ' OR '.join(f'(generation={side}.generation AND surface={side}.surface)' for side in sides)
            name = 'published_' + table + '_' + operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table} '
                f'WHEN EXISTS(SELECT 1 FROM {LINKS} WHERE {condition}) '
                "BEGIN SELECT RAISE(ABORT,'Published report generation is retained'); END")


def validate(conn):
    from ..migrations.current import _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible report publication object: ' + name)


def validate_installed(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recommendation_report_publications' AND type='table'").fetchone():
        return
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    JOB.validate(conn)
    validate(conn)
    if not conn.execute(f'SELECT 1 FROM {HEAD} WHERE id=1').fetchone():
        raise MigrationError('Report publication has no committed selection')


def active(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recommendation_report_publications' AND type='table'").fetchone():
        return False
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return False
    return conn.execute(f'SELECT 1 FROM {HEAD} p JOIN {JOB.state} s ON s.id=p.id '
                        'WHERE p.id=1 AND p.prepared_revision=s.input_revision').fetchone() is not None


def selected(conn, surface):
    return conn.execute(f'SELECT g.* FROM {HEAD} p JOIN {LINKS} h ON h.revision=p.prepared_revision '
        f'JOIN {preparation.HEAD} g ON g.generation=h.generation AND g.surface=h.surface '
        'WHERE p.id=1 AND h.surface=?', (surface,)).fetchone()


def _capture(conn):
    heads = conn.execute(f'SELECT g.* FROM prepared_report_heads h JOIN {preparation.HEAD} g '
                        'USING(generation,surface) ORDER BY surface LIMIT 5').fetchall()
    if len(heads) > 4 or any(row['surface'] not in reports.SURFACES for row in heads):
        raise MigrationError('Report publication exceeds its fixed owner set')
    result = []
    for head in heads:
        parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {preparation.PARTS} '
            'WHERE generation=? AND surface=? ORDER BY position LIMIT 65', (head['generation'], head['surface'])).fetchall()
        usage = conn.execute('SELECT format,field,value FROM prepared_report_usage '
            'WHERE generation=? AND surface=? ORDER BY format,field LIMIT 10', (head['generation'], head['surface'])).fetchall()
        result.append((dict(head), list(map(tuple, parts)), list(map(tuple, usage))))
    return result


def _verify(captured, originals):
    if {head['surface'] for head, _, _ in captured} != set(originals):
        raise MigrationError('Prepared reports do not cover the installed documents')
    for head, parts, usage in captured:
        if [part[0] for part in parts] != list(range(head['part_count'])):
            raise MigrationError('Prepared report parts are incomplete')
        raw = ''.join(files.read(*part[1:], surface=head['surface'], chunk_key=str(part[0])) for part in parts)
        if (len(raw.encode()) != head['total_bytes']
                or hashlib.sha256(raw.encode()).hexdigest() != head['content_digest']):
            raise MigrationError('Prepared report differs from its original digest')
        source, built_at = originals[head['surface']]
        if raw.encode() != source:
            raise MigrationError('Prepared report differs from the installed document')
        plan = reports.prepare(head['surface'], raw, built_at)
        if tuple(head[name] for name in preparation.METADATA) != plan.metadata or usage != sorted(plan.usage):
            raise MigrationError('Prepared report fields differ from the verified document')


def publish(conn, *, revision, clock=time.time):
    if conn.in_transaction:
        raise MigrationError('Publication requires an idle connection')
    try:
        conn.execute('BEGIN')
        JOB._version(conn)
        JOB.validate(conn)
        progress = JOB.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.prepared_revision != revision
                or progress.input_revision != revision or conn.execute(f'SELECT 1 FROM {JOB.changes} LIMIT 1').fetchone()):
            raise MigrationError('Report publication requires the requested reconciled preparation revision')
        present = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone()
        if present:
            validate(conn)
            if not conn.execute(f'SELECT 1 FROM {HEAD} WHERE id=1').fetchone():
                raise MigrationError('Report publication has no committed selection')
        captured = _capture(conn)
        owners = ','.join("'"+surface+"'" for surface in sorted(reports.SURFACES))
        originals = {row[0]: (bytes(row[1]), row[2]) for row in conn.execute(
            f"SELECT surface,COALESCE(substr(CAST(payload AS BLOB),1,{reports.MAX_BYTES + 1}),X''),built_at "
            f'FROM main.rec_proposals WHERE surface IN ({owners})')}
        if any(len(raw) > reports.MAX_BYTES for raw, _ in originals.values()):
            raise MigrationError('Report exceeds its publication byte budget')
        conn.rollback()
        # File integrity and document decoding never hold a DB transaction.
        _verify(captured, originals)
        conn.execute('BEGIN IMMEDIATE')
        JOB.validate(conn)
        if JOB.progress(conn) != progress or _capture(conn) != captured:
            raise MigrationError('Report preparation changed during publication verification')
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
            existing = conn.execute(f'SELECT prepared_revision FROM {HEAD} WHERE id=1').fetchone()
            if existing is None:
                raise MigrationError('Report publication has no committed selection')
            if existing[0] == revision:
                conn.rollback()
                return
        else:
            for _, sql in definitions():
                conn.execute(sql)
        conn.executemany(f'INSERT INTO {LINKS} VALUES (?,?,?)',
                         [(revision, head['surface'], head['generation']) for head, _, _ in captured])
        conn.execute(f'INSERT INTO {HEAD} VALUES (1,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET prepared_revision=excluded.prepared_revision,published_at=excluded.published_at',
                     (revision, clock()))
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
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Report publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
