"""Bounded keyset copying and change capture for explicit preparation jobs.

Definitions name a finite set of installed owners and their durable keys. This
module owns only checkpoints and journals; each job owns its destination schema
and conversion. A prepared revision never changes the serving representation.
"""
from dataclasses import asdict, dataclass
import json
import sqlite3
import time

from ..migrations.runner import MigrationError, history, validate_version


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class Progress:
    status: str
    owner: str | None
    cursor: str | None
    processed: int
    input_revision: int
    prepared_revision: int | None
    error: str | None


@dataclass(frozen=True)
class Job:
    name: str
    minimum: int
    maximum: int
    keys: dict
    destinations: tuple
    copy: object
    delete: object
    dependencies: tuple = ()
    filters: dict | None = None
    projections: dict | None = None
    batch_limit: int = 512
    optional_dependencies: tuple = ()
    refresh_highwater: bool = True
    additive_destinations: tuple = ()

    @property
    def state(self):
        return 'prepare_' + self.name + '_state'

    @property
    def sources(self):
        return 'prepare_' + self.name + '_sources'

    @property
    def changes(self):
        return 'prepare_' + self.name + '_changes'

    def _highwater(self, owner):
        keys = self.keys[owner]
        predicate = self._filter(owner)
        return '(SELECT json_array(' + ','.join(keys) + ') FROM main.' + owner + (' WHERE '+predicate if predicate else '') + ' ORDER BY ' + ','.join(k+' DESC' for k in keys) + ' LIMIT 1)'

    def _filter(self, owner, side=''):
        predicate = (self.filters or {}).get(owner)
        return predicate.format(prefix=side+'.' if side else '') if predicate else None

    def definitions(self, conn=None):
        yield self.state, f'''CREATE TABLE {self.state} (
            id INTEGER PRIMARY KEY CHECK(id=1), source_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('copying','reconciling','prepared','failed')),
            owner TEXT, cursor TEXT CHECK(cursor IS NULL OR json_valid(cursor)),
            processed INTEGER NOT NULL DEFAULT 0 CHECK(processed>=0),
            input_revision INTEGER NOT NULL DEFAULT 0 CHECK(input_revision>=0),
            prepared_revision INTEGER, updated_at REAL NOT NULL, error TEXT,
            CHECK((status='failed')=(error IS NOT NULL)),
            CHECK(status<>'prepared' OR (prepared_revision IS NOT NULL AND prepared_revision=input_revision))
        )'''
        yield self.sources, f'''CREATE TABLE {self.sources} (
            owner TEXT PRIMARY KEY, highwater TEXT CHECK(highwater IS NULL OR json_valid(highwater))
        ) WITHOUT ROWID'''
        yield self.changes, f'''CREATE TABLE {self.changes} (
            owner TEXT NOT NULL REFERENCES {self.sources}(owner),
            source_key TEXT NOT NULL CHECK(json_valid(source_key)),
            PRIMARY KEY(owner,source_key)
        ) WITHOUT ROWID'''
        yield from self.destinations
        yield from self.additive_destinations
        invalidation = (f'UPDATE {self.state} SET input_revision=input_revision+1,prepared_revision=NULL,'
            "status=CASE WHEN status='prepared' THEN 'reconciling' ELSE status END WHERE id=1;")
        for owner, keys in self.keys.items():
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
                # An outer UPSERT can override INSERT OR IGNORE inside a
                # trigger. A real UPSERT keeps repeated keys coalesced without
                # making ordinary source UPSERTs fail their journal constraint.
                journal = ''.join(f'INSERT INTO {self.changes}(owner,source_key) '
                    f"VALUES ('{owner}',json_array(" + ','.join(side+'.'+k for k in keys) +
                    ')) ON CONFLICT(owner,source_key) DO UPDATE SET source_key=excluded.source_key;' for side in sides)
                name = f'prepare_{self.name}_{owner}_{operation.lower()}'
                condition = ' OR '.join(self._filter(owner, side) for side in sides) if self._filter(owner) else None
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner}' +
                             (' WHEN '+condition if condition else '') + ' BEGIN ' + invalidation + journal + ' END')
        # Dependent owner changes can alter normalization without changing a
        # source row. Jobs whose installed keys can change during a historical
        # upgrade defer their finite boundary refresh to the explicit worker;
        # these triggers must not query a retired source column.
        reset = (''.join(f'UPDATE {self.sources} SET highwater={self._highwater(owner)} WHERE owner=\'{owner}\';'
                         for owner in self.keys) if self.refresh_highwater else '')
        first = next(iter(self.keys))
        optional = tuple(owner for owner in self.optional_dependencies if conn is not None and
            conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone())
        for owner in (*self.dependencies, *optional):
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = f'prepare_{self.name}_dependency_{owner}_{operation.lower()}'
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN ' + invalidation +
                    f"UPDATE {self.state} SET owner='{first}',cursor=NULL,"
                    "status=CASE WHEN status='failed' THEN status ELSE 'copying' END WHERE id=1;" + reset + ' END')

    def _version(self, conn):
        from ..migrations.registry import MIGRATIONS
        version = history(conn, MIGRATIONS)
        if not self.minimum <= version <= self.maximum:
            raise MigrationError(f'{self.name} preparation requires installed layout {self.minimum}–{self.maximum}')
        return version

    def validate(self, conn):
        from ..migrations.current import _tokens
        for name, sql in self.definitions(conn):
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is None and name in dict(self.additive_destinations):
                continue
            if row is None or _tokens(row[0]) != _tokens(sql):
                raise MigrationError('Incompatible preparation object: ' + name)

    def progress(self, conn):
        row = conn.execute(f'SELECT status,owner,cursor,processed,input_revision,prepared_revision,error '
                           f'FROM {self.state} WHERE id=1').fetchone()
        return Progress(*row) if row else None

    def install(self, conn, *, clock=time.time):
        if conn.in_transaction:
            raise MigrationError('Preparation requires an idle connection')
        try:
            conn.execute('BEGIN IMMEDIATE')
            version = self._version(conn)
            validate_version(conn, version, foreign_keys=False, data=False)
            if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.state,)).fetchone():
                # A genuine historical upgrade may add an optional dependency.
                # Install its capture triggers and withdraw the previous proof
                # atomically. Existing objects still require exact validation.
                added = False
                optional_names = {f'prepare_{self.name}_dependency_{owner}_{operation}'
                    for owner in self.optional_dependencies for operation in ('insert', 'update', 'delete')}
                optional_names.update(name for name, _ in self.additive_destinations)
                for name, sql in self.definitions(conn):
                    if name in optional_names and not conn.execute(
                            'SELECT 1 FROM main.sqlite_schema WHERE name=?', (name,)).fetchone():
                        conn.execute(sql)
                        added = True
                if added:
                    for owner in self.keys:
                        conn.execute(f'UPDATE {self.sources} SET highwater={self._highwater(owner)} WHERE owner=?', (owner,))
                    conn.execute(f'UPDATE {self.state} SET input_revision=input_revision+1,prepared_revision=NULL,'
                        "status=CASE WHEN status='failed' THEN status ELSE 'copying' END,owner=?,cursor=NULL,updated_at=? WHERE id=1",
                        (next(iter(self.keys)), clock()))
                self.validate(conn)
            else:
                for _, sql in self.definitions(conn):
                    conn.execute(sql)
                for owner in self.keys:
                    conn.execute(f'INSERT INTO {self.sources}(owner,highwater) SELECT ?,{self._highwater(owner)}', (owner,))
                conn.execute(f'INSERT INTO {self.state}(id,source_version,status,owner,updated_at) '
                             "VALUES (1,?,'copying',?,?)", (version, next(iter(self.keys)), clock()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.progress(conn)

    def _read(self, conn, owner, key):
        keys = self.keys[owner]
        predicate = self._filter(owner)
        columns = ','.join((self.projections or {}).get(owner, ('*',)))
        # Use the owner's indexed equality first, then compare the exact
        # journal key. NOCASE keys can be renamed without changing identity;
        # the old spelling must withdraw its staging row rather than read the
        # replacement spelling and leave two prepared rows behind.
        return conn.execute('SELECT ' + columns + ' FROM main.' + owner + ' WHERE ' +
                            ' AND '.join(k+'=?' for k in keys) + ' AND json_array(' + ','.join(keys) + ')=?' +
                            (' AND ('+predicate+')' if predicate else ''), (*json.loads(key), key)).fetchone()

    def scan_highwaters(self, conn, current):
        """Capture a finite boundary when an explicit dependency rescan starts.

        Include keys already copied through the journal beyond the previous
        boundary. File workers retain this snapshot until their revision fence
        admits it, so planning never publishes a boundary for stale work.
        """
        if self.refresh_highwater or current.owner != next(iter(self.keys)) or current.cursor is not None:
            return None
        return {owner: conn.execute('SELECT ' + self._highwater(owner)).fetchone()[0] for owner in self.keys}

    def save_highwaters(self, conn, highwaters):
        if highwaters is not None:
            conn.executemany(f'UPDATE {self.sources} SET highwater=? WHERE owner=?',
                             ((boundary, owner) for owner, boundary in highwaters.items()))

    def select(self, conn, current, size, *, highwaters=None):
        """Read bounded work without changing its checkpoint or journal.

        File workers can release this snapshot, prepare durable files, then
        recheck the captured progress under their writer reservation.
        """
        size = min(size, self.batch_limit)
        if current.owner is None:
            rows = conn.execute(f'SELECT owner,source_key FROM {self.changes} ORDER BY owner,source_key LIMIT ?', (size,)).fetchall()
            work = []
            for owner, key in rows:
                row = self._read(conn, owner, key)
                work.append((owner, key, dict(row) if row is not None else None))
            return None, None, work
        owner = current.owner
        keys = self.keys[owner]
        order = ','.join(keys)
        comparison = '(' + order + ')'
        placeholders = '(' + ','.join('?' for _ in keys) + ')'
        highwater = (highwaters[owner] if highwaters is not None else
                     conn.execute(f'SELECT highwater FROM {self.sources} WHERE owner=?', (owner,)).fetchone()[0])
        parameters = json.loads(highwater) if highwater is not None else [None]*len(keys)
        columns = ','.join((self.projections or {}).get(owner, ('*',)))
        query = f'SELECT json_array({order}) AS _preparation_key,{columns} FROM main.{owner} WHERE {comparison}<={placeholders}'
        predicate = self._filter(owner)
        if predicate:
            query += ' AND ('+predicate+')'
        if current.cursor is not None:
            query += f' AND {comparison}>{placeholders}'
            parameters += json.loads(current.cursor)
        rows = conn.execute(query+' ORDER BY '+order+' LIMIT ?', (*parameters, size)).fetchall()
        cursor = current.cursor
        work = []
        for row in rows:
            value = dict(row)
            cursor = value.pop('_preparation_key')
            work.append((owner, cursor, value))
        if len(rows) < size:
            owners = tuple(self.keys)
            index = owners.index(owner)+1
            owner, cursor = (owners[index] if index < len(owners) else None), None
        return owner, cursor, work

    def _batch(self, conn, current, size):
        self.save_highwaters(conn, self.scan_highwaters(conn, current))
        owner, cursor, work = self.select(conn, current, size)
        for source, key, row in work:
            if row is None:
                self.delete(conn, source, key)
            else:
                self.copy(conn, source, key, row)
            conn.execute(f'DELETE FROM {self.changes} WHERE owner=? AND source_key=?', (source, key))
        return owner, cursor, len(work)

    def step(self, conn, *, batch_size=128, retry=False, clock=time.time):
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError('Preparation batch size must be from 1 through 512')
        if conn.in_transaction:
            raise MigrationError('Preparation requires an idle connection')
        try:
            conn.execute('BEGIN IMMEDIATE')
            self._version(conn)
            self.validate(conn)
            current = self.progress(conn)
            if current is None:
                raise MigrationError('Preparation is not initialized')
            if current.status == 'failed' and not retry:
                raise MigrationError('Repair the input and retry explicitly: ' + current.error)
            if current.status == 'prepared':
                conn.rollback()
                return current
            conn.execute('SAVEPOINT preparation_batch')
            try:
                owner, cursor, count = self._batch(conn, current, batch_size)
                empty = owner is None and not conn.execute(f'SELECT 1 FROM {self.changes} LIMIT 1').fetchone()
                status = 'prepared' if empty else 'copying' if owner is not None else 'reconciling'
                conn.execute(f'UPDATE {self.state} SET owner=?,cursor=?,processed=processed+?,status=?,error=NULL,'
                    'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                    (owner, cursor, count, status, int(empty), clock()))
            except Exception as exc:
                conn.execute('ROLLBACK TO preparation_batch')
                conn.execute(f"UPDATE {self.state} SET status='failed',error=?,prepared_revision=NULL,updated_at=? WHERE id=1",
                             (str(exc)[:2000], clock()))
                conn.commit()
                raise
            conn.execute('RELEASE preparation_batch')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.progress(conn)


def main(job, argv=None):
    """Explicit bounded invocation; status opens read-only and missing DBs fail."""
    import argparse
    from contextlib import closing
    from pathlib import Path
    def number(value):
        result = int(value)
        if not 1 <= result <= 512:
            raise argparse.ArgumentTypeError('must be from 1 through 512')
        return result
    parser = argparse.ArgumentParser(description=job.name + ' preparation; installed owners remain authoritative')
    parser.add_argument('database', type=Path)
    parser.add_argument('--batches', type=number, default=1)
    parser.add_argument('--batch-size', type=number, default=128)
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args(argv)
    try:
        mode = 'ro' if args.status else 'rw'
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode='+mode, uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.status:
                present = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (job.state,)).fetchone()
                result = job.progress(conn) if present else None
            else:
                result = job.install(conn)
                for _ in range(args.batches):
                    result = job.step(conn, batch_size=args.batch_size, retry=args.retry)
                    if result.status == 'prepared':
                        break
            print(_json(asdict(result) if result else {'status': 'uninitialized'}))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'{job.name} preparation failed: {exc}\n')
    return 0
