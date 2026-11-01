"""Publish verified sync reports independently of the three scalar sync stamps."""
import hashlib
import json
import math
import time

from .. import sync_state
from ..migrations.runner import MigrationError
from . import library_sync as preparation
from .document_publication import DocumentPublication, Proof, main as run
from .selection import invalidators


def verify_rows(conn, header, described):
    kind, phase, has_attempt, finished_at, items = described
    expected = (kind, phase, int(has_attempt), float(finished_at) if finished_at is not None else None, len(items))
    if tuple(header[field] for field in ('source_kind','phase','has_attempt_time','finished_at','item_count')) != expected:
        raise MigrationError('Prepared sync report fields differ from its original')
    rows = conn.execute('SELECT * FROM '+preparation.CURSORS+' WHERE source=? AND generation=? ORDER BY position',
                        (header['source'], header['generation']))
    for _, position, item in items:
        fields = list(preparation.reference(conn, item))
        for offset in (9, 10, 12):  # SQLite REAL affinity, including large integer times.
            if fields[offset] is not None:
                fields[offset] = float(fields[offset])
        row = rows.fetchone()
        if row is None or tuple(row) != (header['source'], header['generation'], position, *fields):
            raise MigrationError('Prepared sync cursor differs from its original or references')
    if rows.fetchone() is not None:
        raise MigrationError('Prepared sync report contains extra cursors')


RESULT = DocumentPublication(sync_state.KEY, preparation, preparation.RESULT_JOB, sync_state.available, verify_rows)
GENERATIONS = 'published_library_sync_stamp_generations'
VALUES = 'published_library_sync_stamp_values'
HEAD = 'published_library_sync_stamp_head'
EPOCH = 'published_library_sync_stamp_custody_epoch'
NAME = 'library-sync-stamps'


def definitions():
    yield GENERATIONS, f'''CREATE TABLE {GENERATIONS} (
        revision INTEGER PRIMARY KEY CHECK(revision>=0),published_at REAL NOT NULL)'''
    yield VALUES, f'''CREATE TABLE {VALUES} (
        revision INTEGER NOT NULL REFERENCES {GENERATIONS}(revision) DEFERRABLE INITIALLY DEFERRED,
        source TEXT NOT NULL CHECK(source IN ('last_sync_at','last_sync_attempt_at','last_sync_outcome')),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        timestamp REAL,outcome TEXT,source_digest TEXT NOT NULL,
        PRIMARY KEY(revision,source),
        FOREIGN KEY(source,source_digest) REFERENCES {preparation.ORIGINAL_STAMPS}(source,digest)) WITHOUT ROWID'''
    yield HEAD, f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name={NAME!r}),revision INTEGER NOT NULL REFERENCES {GENERATIONS}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
    yield EPOCH, f'''CREATE TABLE {EPOCH} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'''
    yield VALUES+'_custody', f'CREATE INDEX {VALUES}_custody ON {VALUES}(source,source_digest)'
    for owner in (GENERATIONS, VALUES):
        for operation in ('UPDATE', 'DELETE'):
            name = owner+'_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                "BEGIN SELECT RAISE(ABORT,'Published sync stamp generations require future cleanup'); END")
    name = VALUES+'_insert'
    yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{VALUES} '
        f'WHEN EXISTS(SELECT 1 FROM {GENERATIONS} WHERE revision=NEW.revision) '
        "BEGIN SELECT RAISE(ABORT,'Published sync stamp custody is retained'); END")
    for owner in (preparation.STAMPS, preparation.ORIGINAL_STAMPS):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            if owner == preparation.ORIGINAL_STAMPS:
                sides = ('OLD','NEW') if operation == 'UPDATE' else ('NEW',) if operation == 'INSERT' else ('OLD',)
                condition = ' OR '.join(f'(source={side}.source AND source_digest={side}.digest)' for side in sides)
                name = owner+'_published_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                    f'WHEN EXISTS(SELECT 1 FROM {VALUES} WHERE {condition}) '
                    "BEGIN SELECT RAISE(ABORT,'Published sync stamp originals are retained'); END")
            name = owner+'_custody_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                f'BEGIN UPDATE {EPOCH} SET revision=revision+1 WHERE id=1; END')
    yield from invalidators(HEAD, {NAME:preparation.STAMP_JOB})


class StampPublication:
    source = NAME
    worker = preparation.STAMP_JOB

    def validate(self, conn):
        from ..migrations.current import _tokens
        for name, sql in definitions():
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is None or _tokens(row[0]) != _tokens(sql):
                raise MigrationError('Incompatible sync stamp publication object: '+name)
        if not conn.execute('SELECT 1 FROM '+EPOCH+' WHERE id=1').fetchone():
            raise MigrationError('Sync stamp publication requires its custody revision')

    def validate_installed(self, conn):
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            return
        self.validate(conn)
        if not sync_state.available(conn):
            self.worker.validate(conn)
        if conn.execute('SELECT 1 FROM '+HEAD+' h LEFT JOIN '+GENERATIONS+' g ON g.revision=h.revision '
                'WHERE g.revision IS NULL LIMIT 1').fetchone():
            raise MigrationError('Sync stamp selection requires its committed generation')
        if conn.execute('SELECT 1 FROM '+HEAD+' h JOIN '+VALUES+' v ON v.revision=h.revision LEFT JOIN '+
                preparation.ORIGINAL_STAMPS+' o ON o.source=v.source AND o.digest=v.source_digest '
                'WHERE o.digest IS NULL LIMIT 1').fetchone():
            raise MigrationError('Sync stamp selection requires its retained originals')

    def install(self, conn):
        if conn.in_transaction:
            raise MigrationError('Sync stamp publication requires an idle connection')
        try:
            conn.execute('BEGIN IMMEDIATE')
            self.worker._version(conn)
            self.worker.validate(conn)
            if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
                self.validate(conn)
            else:
                for _, sql in definitions():
                    conn.execute(sql)
                conn.execute('INSERT INTO '+EPOCH+' VALUES (1,0)')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def selected(self, conn):
        if sync_state.available(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+HEAD+' WHERE name=? AND active=1', (NAME,)).fetchone()
        return row[0] if row else None

    def value(self, conn, key, revision, default=None):
        row = conn.execute('SELECT o.raw_value FROM '+VALUES+' v JOIN '+preparation.ORIGINAL_STAMPS+
            ' o ON o.source=v.source AND o.digest=v.source_digest WHERE v.revision=? AND v.source=?', (revision, key)).fetchone()
        return row[0] if row is not None else default

    def proof(self, conn, revision):
        progress = self.worker.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.input_revision != revision
                or progress.prepared_revision != revision
                or conn.execute('SELECT 1 FROM '+self.worker.changes+' LIMIT 1').fetchone()):
            raise MigrationError('Sync stamp publication requires its reconciled preparation revision')
        values = tuple(map(tuple, conn.execute('SELECT * FROM '+preparation.STAMPS+' ORDER BY source LIMIT 4')))
        return Proof(progress, values, conn.execute('SELECT revision FROM '+EPOCH+' WHERE id=1').fetchone()[0])

    def verify(self, conn, revision):
        if conn.in_transaction:
            raise MigrationError('Sync stamp verification requires an idle connection')
        try:
            conn.execute('BEGIN')
            self.worker._version(conn)
            self.worker.validate(conn)
            self.validate(conn)
            captured = self.proof(conn, revision)
            originals = dict(conn.execute('SELECT s.source,o.raw_value FROM '+preparation.STAMPS+' s JOIN '+
                preparation.ORIGINAL_STAMPS+' o ON o.source=s.source AND o.digest=s.source_digest'))
            inputs = conn.execute("SELECT key,value IS NULL,COALESCE(substr(CAST(value AS BLOB),1,?),X'') "
                'FROM main.settings WHERE key IN (?,?,?) ORDER BY key',
                (preparation.STAMP_BYTES+1, *sync_state.STAMPS)).fetchall()
        finally:
            conn.rollback()
        # At most three bounded scalars. Decode/hash without a SQLite transaction.
        raw = {key:None if null else bytes(value).decode('utf-8') for key, null, value in inputs}
        if originals != raw or len(captured.header) != len(raw):
            raise MigrationError('Prepared sync stamps differ from their installed originals')
        expected = []
        for key, value in sorted(raw.items()):
            if value is not None and len(value.encode()) > preparation.STAMP_BYTES:
                raise MigrationError('Sync stamp exceeds its publication byte budget')
            kind = 'null' if value is None else 'blank' if value == '' else 'recorded'
            parsed = None if kind != 'recorded' else value if key == 'last_sync_outcome' else float(value)
            if parsed is not None and (parsed not in sync_state.STATUSES if key == 'last_sync_outcome' else not math.isfinite(parsed)):
                raise ValueError('Unsupported retained sync stamp')
            digest = hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()
            expected.append((key, kind, None if key == 'last_sync_outcome' else parsed,
                             parsed if key == 'last_sync_outcome' else None, digest))
        if tuple(expected) != captured.header:
            raise MigrationError('Prepared sync stamp fields differ from their originals')
        return captured

    def publish(self, conn, *, revision, clock=time.time):
        if type(revision) is not int or revision < 0:
            raise ValueError('Sync stamp publication requires a nonnegative revision')
        self.install(conn)
        captured = self.verify(conn, revision)
        try:
            conn.execute('BEGIN IMMEDIATE')
            self.worker._version(conn)
            self.worker.validate(conn)
            self.validate(conn)
            if self.proof(conn, revision) != captured:
                raise MigrationError('Sync stamps changed after verification; reconcile before publication')
            if not conn.execute('SELECT 1 FROM '+GENERATIONS+' WHERE revision=?', (revision,)).fetchone():
                conn.executemany('INSERT INTO '+VALUES+' VALUES (?,?,?,?,?,?)', [(revision, *row) for row in captured.header])
                conn.execute('INSERT INTO '+GENERATIONS+' VALUES (?,?)', (revision, clock()))
            conn.execute('INSERT INTO '+HEAD+' VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1',
                         (NAME, revision))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


STAMPS = StampPublication()
OWNERS = {'result':RESULT, 'stamps':STAMPS}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    return run(OWNERS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
