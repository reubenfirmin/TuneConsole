"""Publish one ordered source document after bulk proof outside the writer.

The family supplies its original-file reader and typed-row verifier. This common
boundary retains custody, fences continuing writes and keeps startup validation
finite; it never changes which representation compatible writers own.
"""
from dataclasses import dataclass
import hashlib
import time

from ..migrations.runner import MigrationError
from .selection import invalidators


@dataclass(frozen=True)
class Proof:
    progress: object
    header: tuple | None
    epoch: int


@dataclass(frozen=True)
class DocumentPublication:
    source: str
    preparation: object
    worker: object
    native: object
    verify_rows: object
    generation_column: str = 'generation'
    verify_files: object | None = None
    immutable_originals: tuple = ()

    @property
    def job(self):
        return self.worker.definition

    @property
    def tables(self):
        prefix = self.preparation.tables(self.source)[0].removesuffix('_generations').replace('prepared_', 'published_', 1)
        return prefix+'_generations', prefix+'_head', prefix+'_custody_epoch'

    def definitions(self):
        generations, head, epoch = self.tables
        prepared, _, current = self.preparation.tables(self.source)
        yield generations, f'''CREATE TABLE {generations} (
            revision INTEGER PRIMARY KEY CHECK(revision>=0),source TEXT NOT NULL CHECK(source={self.source!r}),
            generation INTEGER,published_at REAL NOT NULL,
            FOREIGN KEY(source,generation) REFERENCES {prepared}(source,{self.generation_column}))'''
        yield head, f'''CREATE TABLE {head} (
            name TEXT PRIMARY KEY CHECK(name={self.source!r}),revision INTEGER NOT NULL REFERENCES {generations}(revision),
            active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
        yield epoch, f'''CREATE TABLE {epoch} (
            id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'''
        yield generations+'_custody', f'CREATE INDEX {generations}_custody ON {generations}(generation)'
        for operation in ('UPDATE', 'DELETE'):
            name = generations+'_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{generations} '
                "BEGIN SELECT RAISE(ABORT,'Published document generations require future cleanup'); END")
        for owner, _ in self.job.destinations:
            if owner in self.immutable_originals:
                for operation in ('UPDATE', 'DELETE'):
                    name = owner+'_published_'+operation.lower()
                    yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                        "BEGIN SELECT RAISE(ABORT,'Published document originals require future cleanup'); END")
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                if owner != current and owner not in self.immutable_originals:
                    sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('NEW',) if operation == 'INSERT' else ('OLD',)
                    condition = ' OR '.join('generation='+side+'.'+self.generation_column for side in sides)
                    name = owner+'_published_'+operation.lower()
                    yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                        f'WHEN EXISTS(SELECT 1 FROM {generations} WHERE {condition}) '
                        "BEGIN SELECT RAISE(ABORT,'Published document custody is retained'); END")
                name = owner+'_custody_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                    f'BEGIN UPDATE {epoch} SET revision=revision+1 WHERE id=1; END')
        yield from invalidators(head, {self.source: self.job})

    def validate(self, conn):
        from ..migrations.current import _tokens
        from ..entity_names import predecessor_definition as transform
        renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone()
        for name, sql in self.definitions():
            expected = transform(sql) if renamed else sql
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is None or _tokens(row[0]) != _tokens(expected):
                raise MigrationError('Incompatible document publication object: '+name)
        if not conn.execute('SELECT 1 FROM '+self.tables[2]+' WHERE id=1').fetchone():
            raise MigrationError('Document publication requires its custody revision')

    def validate_installed(self, conn):
        generations, head, _ = self.tables
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (head,)).fetchone():
            return
        self.validate(conn)
        if not self.native(conn):
            self.validate_preparation(conn)
        prepared = self.preparation.tables(self.source)[0]
        if conn.execute('SELECT 1 FROM '+head+' h LEFT JOIN '+generations+' g ON g.revision=h.revision '
                'LEFT JOIN '+prepared+' p ON p.source=g.source AND p.'+self.generation_column+'=g.generation '
                'WHERE g.revision IS NULL OR (g.generation IS NOT NULL AND p.'+self.generation_column+' IS NULL) LIMIT 1').fetchone():
            raise MigrationError('Document selection requires its committed generation')

    def validate_preparation(self, conn):
        self.job.validate(conn)

    def install(self, conn):
        if conn.in_transaction:
            raise MigrationError('Document publication requires an idle connection')
        def installed():
            self.job._version(conn)
            self.job.validate(conn)
            exists = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.tables[1],)).fetchone()
            if exists:
                self.validate(conn)
            return exists
        try:
            conn.execute('BEGIN')
            exists = installed()
            conn.rollback()
            if exists:
                return
            conn.execute('BEGIN IMMEDIATE')
            if not installed():
                for _, sql in self.definitions():
                    conn.execute(sql)
                conn.execute('INSERT INTO '+self.tables[2]+' VALUES (1,0)')
            conn.commit()
        finally:
            conn.rollback()

    def selected(self, conn):
        if self.native(conn):
            return None
        _, head, _ = self.tables
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (head,)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+head+' WHERE name=? AND active=1', (self.source,)).fetchone()
        return row[0] if row else None

    def value(self, conn, revision, default=None):
        row = conn.execute('SELECT generation FROM '+self.tables[0]+' WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise MigrationError('Published document generation is missing')
        if row[0] is None:
            return default
        captured = conn.execute('SELECT * FROM '+self.preparation.tables(self.source)[0]+
            ' WHERE source=? AND '+self.generation_column+'=?', (self.source, row[0])).fetchone()
        if captured is None:
            raise MigrationError('Published document original is missing')
        return self.preparation.read_original(conn, captured)

    def capture(self, conn):
        prepared, _, current = self.preparation.tables(self.source)
        return conn.execute('SELECT g.* FROM '+current+' h JOIN '+prepared+
            ' g USING(source,'+self.generation_column+') WHERE h.source=?', (self.source,)).fetchone()

    def proof(self, conn, revision):
        progress = self.worker.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.input_revision != revision
                or progress.prepared_revision != revision
                or conn.execute('SELECT 1 FROM '+self.job.changes+' LIMIT 1').fetchone()):
            raise MigrationError('Document publication requires its reconciled preparation revision')
        header = self.capture(conn)
        return Proof(progress, tuple(header) if header is not None else None,
            conn.execute('SELECT revision FROM '+self.tables[2]+' WHERE id=1').fetchone()[0])

    def read_source(self, conn):
        return conn.execute("SELECT value IS NULL,COALESCE(substr(CAST(value AS BLOB),1,?),X'') "
            'FROM main.settings WHERE key=?', (self.preparation.MAX_BYTES+1, self.source)).fetchone()

    def verify(self, conn, revision):
        if conn.in_transaction:
            raise MigrationError('Document verification requires an idle connection')
        try:
            conn.execute('BEGIN')
            self.job._version(conn)
            self.job.validate(conn)
            self.validate(conn)
            captured = self.proof(conn, revision)
            header = self.capture(conn)
            source = self.read_source(conn)
            raw = bytes(source[1]).decode('utf-8') if source and not source[0] else None
        finally:
            conn.rollback()
        if bool(source) != (header is not None):
            raise MigrationError('Prepared document differs from its installed source presence')
        # The file reader finishes its bounded manifest query before any file I/O.
        if header is not None and self.preparation.read_original(conn, header) != raw:
            raise MigrationError('Prepared document differs from its installed original')
        # Decode without holding a database transaction, including a read snapshot.
        described = self.preparation.describe(self.source, raw) if header is not None else None
        if header is not None and self.verify_files is not None:
            described = self.verify_files(conn, header, described)
        try:
            conn.execute('BEGIN')
            if self.proof(conn, revision) != captured:
                raise MigrationError('Document changed during verification; reconcile before publication')
            if header is not None:
                self.verify_rows(conn, header, described)
            return captured
        finally:
            conn.rollback()

    def publish(self, conn, *, revision, clock=time.time):
        if type(revision) is not int or revision < 0:
            raise ValueError('Document publication requires a nonnegative revision')
        self.install(conn)
        captured = self.verify(conn, revision)
        try:
            conn.execute('BEGIN IMMEDIATE')
            self.job._version(conn)
            self.job.validate(conn)
            self.validate(conn)
            if self.proof(conn, revision) != captured:
                raise MigrationError('Document changed after verification; reconcile before publication')
            generations, head, _ = self.tables
            if not conn.execute('SELECT 1 FROM '+generations+' WHERE revision=?', (revision,)).fetchone():
                header = self.capture(conn)
                conn.execute('INSERT INTO '+generations+' VALUES (?,?,?,?)',
                    (revision, self.source, header[self.generation_column] if header is not None else None, clock()))
            conn.execute('INSERT INTO '+head+' VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1',
                (self.source, revision))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def repair_parts(conn, progress, *, source, tables, prepare, max_bytes, max_parts):
    """Restore only exact missing files; never rewrite selected manifests or rows."""
    header, parts, current = tables
    try:
        conn.execute('BEGIN')
        captured = conn.execute('SELECT g.* FROM '+current+' h JOIN '+header+
            ' g USING(source,generation) WHERE h.source=?', (source,)).fetchone()
        if captured is None:
            return
        row = conn.execute('SELECT key,CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,?),X\'\') END AS value '
            'FROM main.settings WHERE key=?', (max_bytes+1, source)).fetchone()
        if row is None or hashlib.sha256(bytes(row['value']) if row['value'] is not None else b'').hexdigest() != captured['raw_digest']:
            return  # Changed source belongs to the ordinary resumable worker.
        retained = conn.execute('SELECT position,filename,digest,byte_size FROM '+parts+
            ' WHERE source=? AND generation=? ORDER BY position LIMIT ?',
            (source, captured['generation'], max_parts+1)).fetchall()
    finally:
        conn.rollback()
    plan = prepare(conn, row)
    expected = [(position, *file) for kind, position, file in plan.units if kind == 'file']
    if list(map(tuple, retained)) != expected:
        raise MigrationError('Document recovery differs from its retained original parts')


def verify_paged_plan(source, header, fields, units):
    """Verify all retained pages against independently reconstructed members."""
    from ..evidence_ledger import canonical
    from . import document_plan
    plan = document_plan.load(source, tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    if canonical(plan.fields) != canonical(fields) or len(plan.units) != len(units):
        raise MigrationError('Document plan metadata differs from its original')
    for position, expected in enumerate(units):
        if canonical(plan.units[position]) != canonical(expected):
            raise MigrationError('Document plan member differs from its original or references')


def main(owners, description, argv=None):
    import argparse
    from contextlib import closing
    import json
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('job', choices=tuple(owners))
    parser.add_argument('database', type=Path)
    parser.add_argument('--revision', type=int, required=True)
    args = parser.parse_args(argv)
    owner = owners[args.job]
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            owner.publish(conn, revision=args.revision)
            print(json.dumps({'source':owner.source, 'revision':owner.selected(conn)}))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Document publication failed: {exc}\n')
    return 0
