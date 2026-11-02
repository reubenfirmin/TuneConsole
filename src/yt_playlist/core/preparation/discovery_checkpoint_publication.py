"""Select retained discovery checkpoints independently of historical conversion.

Each family stages at most sixteen rows per call. Its full source audit runs on
an independent connection without a writer reservation. The final transaction
checks the same source and custody revisions before selecting one immutable set.
"""
from dataclasses import asdict, dataclass
import json
import time

from ..migrations.runner import MigrationError
from . import discovery_checkpoints as preparation


NATIVE = {'fetch-history': 'discovery_fetch_history', 'artist-images': 'discovery_artist_image_state',
          'provider-refresh': 'provider_refresh_frontiers'}


@dataclass(frozen=True)
class Stage:
    id: int
    input_revision: int
    custody_revision: int
    after_source: str | None
    copied: int
    complete: int


@dataclass(frozen=True)
class Publication:
    name: str

    @property
    def job(self):
        return preparation.JOBS[self.name]

    def table(self, suffix):
        return 'published_discovery_checkpoint_'+self.job.name+'_'+suffix

    def native(self, conn):
        return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?",
                            (NATIVE[self.name],)).fetchone() is not None

    def definitions(self):
        epoch, stages, rows, generations, head = (self.table(suffix)
            for suffix in ('epoch', 'stages', 'rows', 'generations', 'head'))
        yield epoch, f'CREATE TABLE {epoch} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
        yield stages, f'''CREATE TABLE {stages} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,input_revision INTEGER NOT NULL,custody_revision INTEGER NOT NULL,
            after_source TEXT,copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
        yield rows, f'''CREATE TABLE {rows} (
            stage INTEGER NOT NULL REFERENCES {stages}(id),source TEXT NOT NULL,
            raw_value TEXT NOT NULL CHECK(length(CAST(raw_value AS BLOB))<={preparation.MAX_BYTES}),
            {preparation.COLUMNS[self.name]},PRIMARY KEY(stage,source)) WITHOUT ROWID'''
        yield generations, f'''CREATE TABLE {generations} (
            revision INTEGER PRIMARY KEY REFERENCES {stages}(id),input_revision INTEGER NOT NULL,published_at REAL NOT NULL)'''
        yield head, f'''CREATE TABLE {head} (
            id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {generations}(revision),
            active INTEGER NOT NULL CHECK(active IN (0,1)))'''
        for owner in (rows, generations, stages):
            for operation in ('UPDATE', 'DELETE'):
                name = owner+'_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '+
                    ('WHEN OLD.complete=1 ' if owner == stages else '')+
                    "BEGIN SELECT RAISE(ABORT,'Discovery checkpoint custody requires future cleanup'); END")
        name = rows+'_insert'
        yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{rows} '
            f'WHEN EXISTS(SELECT 1 FROM {stages} WHERE id=NEW.stage AND complete=1) '
            "BEGIN SELECT RAISE(ABORT,'Completed discovery checkpoint stage is retained'); END")
        for owner, _ in self.job.destinations:
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = self.table(owner+'_'+operation.lower())
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN '
                    f'UPDATE {epoch} SET revision=revision+1 WHERE id=1; '
                    f'UPDATE {head} SET active=0 WHERE active=1; END')
        name = self.table('input_changed')
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{self.job.state} '
            f'WHEN NEW.input_revision<>OLD.input_revision BEGIN UPDATE {head} SET active=0 WHERE active=1; END')

    def validate(self, conn):
        from ..migrations.current import _tokens
        from ..entity_names import predecessor_definition as transform
        renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone()
        for name, sql in self.definitions():
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is None or _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
                raise MigrationError('Incompatible discovery checkpoint publication: '+name)
        if not conn.execute('SELECT 1 FROM '+self.table('epoch')+' WHERE id=1').fetchone():
            raise MigrationError('Discovery checkpoint publication requires its custody revision')

    def validate_installed(self, conn):
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return
        self.validate(conn)
        if not self.native(conn):
            self.job.validate(conn)
        if conn.execute('SELECT 1 FROM '+self.table('head')+' h LEFT JOIN '+self.table('generations')+
                ' g ON g.revision=h.revision LEFT JOIN '+self.table('stages')+
                ' s ON s.id=g.revision WHERE g.revision IS NULL OR s.id IS NULL OR s.complete<>1 '
                'OR g.input_revision<>s.input_revision LIMIT 1').fetchone():
            raise MigrationError('Discovery checkpoint selection requires its completed stage')

    def install(self, conn):
        if conn.in_transaction:
            raise MigrationError('Discovery checkpoint publication requires an idle connection')
        def installed():
            self.job._version(conn)
            self.job.validate(conn)
            exists = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone()
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
                conn.execute('INSERT INTO '+self.table('epoch')+' VALUES (1,0)')
            conn.commit()
        finally:
            conn.rollback()

    def proof(self, conn):
        progress = self.job.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.input_revision != progress.prepared_revision
                or conn.execute('SELECT 1 FROM '+self.job.changes+' LIMIT 1').fetchone()):
            raise MigrationError('Discovery checkpoint publication requires reconciled preparation')
        return progress, conn.execute('SELECT revision FROM '+self.table('epoch')+' WHERE id=1').fetchone()[0]

    def stage(self, conn, identity):
        row = conn.execute('SELECT * FROM '+self.table('stages')+' WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise MigrationError('Discovery checkpoint stage is missing')
        return Stage(*row)

    def latest(self, conn):
        row = conn.execute('SELECT * FROM '+self.table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
        return Stage(*row) if row else None

    def step(self, conn, *, batch_size=16):
        if type(batch_size) is not int or not 1 <= batch_size <= 16:
            raise ValueError('Discovery checkpoint batches require 1 to 16 rows')
        self.install(conn)
        try:
            conn.execute('BEGIN')
            proof, previous = self.proof(conn), self.latest(conn)
            stage = previous if previous and (previous.input_revision, previous.custody_revision) == (
                proof[0].input_revision, proof[1]) else Stage(0, proof[0].input_revision, proof[1], None, 0, 0)
            if stage.complete:
                return stage
            batch = conn.execute('SELECT p.*,substr(CAST(o.raw_value AS BLOB),1,?) raw_value FROM '+
                preparation.TABLES[self.name]+' p LEFT JOIN '+preparation.originals(self.name)+
                ' o ON o.source=p.source AND o.digest=p.source_digest'+
                (' WHERE p.source>?' if stage.after_source is not None else '')+' ORDER BY p.source LIMIT ?',
                (preparation.MAX_BYTES+1, *((stage.after_source,) if stage.after_source is not None else ()), batch_size+1)).fetchall()
            planned = []
            for row in batch[:batch_size]:
                value = dict(row)
                raw = value.pop('raw_value')
                if raw is None or len(raw) > preparation.MAX_BYTES:
                    raise MigrationError('Discovery checkpoint original is missing or exceeds its byte budget')
                if len(json.dumps(value, ensure_ascii=False).encode()) > preparation.MAX_BYTES+preparation.MAX_KEY_BYTES+2048:
                    raise MigrationError('Discovery checkpoint fields exceed their byte budget')
                planned.append({'source':value.pop('source'), 'raw_value':bytes(raw).decode(), **value})
            conn.rollback()
            conn.execute('BEGIN IMMEDIATE')
            if self.proof(conn) != proof or self.latest(conn) != previous:
                raise MigrationError('Discovery checkpoint inputs changed before batch publication; retry')
            if not stage.id:
                identity = conn.execute('INSERT INTO '+self.table('stages')+
                    '(input_revision,custody_revision,copied,complete) VALUES (?,?,0,0)',
                    (stage.input_revision, stage.custody_revision)).lastrowid
                stage = self.stage(conn, identity)
            for row in planned:
                values = {'stage':stage.id, **row}
                conn.execute('INSERT INTO '+self.table('rows')+'('+','.join(values)+') VALUES ('+
                             ','.join('?' for _ in values)+')', tuple(values.values()))
            conn.execute('UPDATE '+self.table('stages')+' SET after_source=?,copied=?,complete=? WHERE id=?',
                (planned[-1]['source'] if planned else stage.after_source, stage.copied+len(planned),
                 int(len(batch)<=batch_size), stage.id))
            stage = self.stage(conn, stage.id)
            conn.commit()
            return stage
        finally:
            conn.rollback()

    def verify(self, conn, identity):
        if conn.in_transaction:
            raise MigrationError('Discovery checkpoint verification requires an idle connection')
        try:
            conn.execute('BEGIN')
            self.job._version(conn)
            self.job.validate(conn)
            self.validate(conn)
            proof, stage = self.proof(conn), self.stage(conn, identity)
            if not stage.complete or (stage.input_revision, stage.custody_revision) != (proof[0].input_revision, proof[1]):
                raise MigrationError('Discovery checkpoint stage is incomplete or changed; reconcile again')
        finally:
            conn.rollback()
        after, count = None, 0
        while True:
            try:
                conn.execute('BEGIN')
                if self.proof(conn) != proof:
                    raise MigrationError('Discovery checkpoint inputs changed during verification; reconcile again')
                batch = conn.execute('SELECT * FROM '+self.table('rows')+' WHERE stage=?'+
                    (' AND source>?' if after is not None else '')+' ORDER BY source LIMIT 16',
                    (identity, *((after,) if after is not None else ()))).fetchall()
                for row in batch:
                    source = self.job._read(conn, 'settings', json.dumps([row['source']], ensure_ascii=False, separators=(',', ':')))
                    if source is None:
                        raise MigrationError('Published discovery checkpoint has no installed source')
                    raw, digest = preparation.original_value(source)
                    expected = preparation.VALUES[self.name](conn, source, raw, digest)
                    prepared = conn.execute('SELECT * FROM '+preparation.TABLES[self.name]+' WHERE source=?', (row['source'],)).fetchone()
                    original = conn.execute('SELECT raw_value FROM '+preparation.originals(self.name)+
                        ' WHERE source=? AND digest=?', (row['source'], digest)).fetchone()
                    actual = tuple(row[key] for key in prepared.keys()) if prepared else None
                    if (prepared is None or tuple(prepared) != expected or actual != expected or row['raw_value'] != raw
                            or original is None or original[0] != raw):
                        raise MigrationError('Discovery checkpoint fields or original differ from installed source')
                count += len(batch)
                if not batch:
                    source_count = conn.execute('SELECT count(*) FROM main.settings WHERE '+self.job._filter('settings')).fetchone()[0]
                    prepared_count = conn.execute('SELECT count(*) FROM '+preparation.TABLES[self.name]).fetchone()[0]
                    if source_count != count or prepared_count != count or stage.copied != count:
                        raise MigrationError('Discovery checkpoint stage omits installed sources or contains extra rows')
                    return proof, stage
                after = batch[-1]['source']
            finally:
                conn.rollback()

    def publish(self, conn, *, stage, clock=time.time):
        if type(stage) is not int or stage <= 0:
            raise ValueError('Discovery checkpoint publication requires a positive stage identity')
        self.install(conn)
        captured = self.verify(conn, stage)
        try:
            conn.execute('BEGIN IMMEDIATE')
            self.job.validate(conn)
            self.validate(conn)
            if (self.proof(conn), self.stage(conn, stage)) != captured:
                raise MigrationError('Discovery checkpoint inputs changed after verification; reconcile again')
            conn.execute('INSERT INTO '+self.table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING',
                         (stage, captured[1].input_revision, clock()))
            conn.execute('INSERT INTO '+self.table('head')+' VALUES (1,?,1) ON CONFLICT(id) '
                         'DO UPDATE SET revision=excluded.revision,active=1', (stage,))
            conn.commit()
        finally:
            conn.rollback()

    def selected(self, conn):
        if self.native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+self.table('head')+' WHERE active=1').fetchone()
        return row[0] if row else None

    def read(self, conn, revision, source):
        return conn.execute('SELECT * FROM '+self.table('rows')+' WHERE stage=? AND source=?', (revision, source)).fetchone()


OWNERS = {name:Publication(name) for name in preparation.JOBS}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=tuple(OWNERS))
    parser.add_argument('database', type=Path)
    parser.add_argument('--publish-stage', type=int)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=16)
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    owner = OWNERS[args.job]
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                owner.publish(conn, stage=args.publish_stage)
                result = {'selected':owner.selected(conn)}
            else:
                for _ in range(args.batches):
                    stage = owner.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'Discovery checkpoint publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
