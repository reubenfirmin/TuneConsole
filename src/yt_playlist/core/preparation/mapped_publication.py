"""Retain a bounded, immutable map before selecting a multi-document owner."""
from dataclasses import dataclass
import time

from ..schema_contract import MigrationError
from ..schema_inspection import definitions as schema_definitions
from .selection import invalidators


@dataclass(frozen=True)
class Stage:
    id: int
    input_revision: int
    custody_revision: int
    after_source: str | None
    copied: int
    complete: int


@dataclass(frozen=True)
class MappedPublication:
    name: str
    worker: object
    header: str
    current: str
    native: object
    verify_document: object
    read_original: object
    source_column: str = 'source'
    generation_column: str = 'preparation_revision'

    @property
    def job(self):
        return self.worker.definition

    @property
    def prefix(self):
        return self.header.removesuffix('_generations')+'_publication'

    def table(self, suffix):
        return self.prefix+'_'+suffix

    def definitions(self):
        epoch, stages, mapping, custody, generations, head = (self.table(suffix)
            for suffix in ('epoch', 'stages', 'map', 'custody', 'generations', 'head'))
        yield epoch, f'''CREATE TABLE {epoch} (
            id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'''
        yield stages, f'''CREATE TABLE {stages} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, input_revision INTEGER NOT NULL CHECK(input_revision>=0),
            custody_revision INTEGER NOT NULL CHECK(custody_revision>=0),after_source TEXT,
            copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
        yield custody, f'''CREATE TABLE {custody} (
            source TEXT NOT NULL,generation INTEGER NOT NULL,PRIMARY KEY(source,generation),
            FOREIGN KEY(source,generation) REFERENCES {self.header}({self.source_column},{self.generation_column})) WITHOUT ROWID'''
        yield mapping, f'''CREATE TABLE {mapping} (
            stage INTEGER NOT NULL REFERENCES {stages}(id),source TEXT NOT NULL,generation INTEGER NOT NULL,
            PRIMARY KEY(stage,source),FOREIGN KEY(source,generation) REFERENCES {custody}(source,generation)) WITHOUT ROWID'''
        yield generations, f'''CREATE TABLE {generations} (
            revision INTEGER PRIMARY KEY REFERENCES {stages}(id),input_revision INTEGER NOT NULL,
            published_at REAL NOT NULL)'''
        yield head, f'''CREATE TABLE {head} (
            name TEXT PRIMARY KEY CHECK(name={self.name!r}),revision INTEGER NOT NULL REFERENCES {generations}(revision),
            active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
        for owner in (custody, mapping, generations):
            for operation in ('UPDATE', 'DELETE'):
                name = owner+'_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                    "BEGIN SELECT RAISE(ABORT,'Retained publication maps require future cleanup'); END")
        for operation in ('UPDATE', 'DELETE'):
            name = stages+'_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{stages} WHEN OLD.complete=1 '
                "BEGIN SELECT RAISE(ABORT,'Completed publication stages are retained'); END")
        name = mapping+'_insert'
        yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{mapping} '
            f'WHEN EXISTS(SELECT 1 FROM {stages} WHERE id=NEW.stage AND complete=1) '
            "BEGIN SELECT RAISE(ABORT,'Completed publication maps are retained'); END")
        for owner, definition in self.job.destinations:
            if not definition.lstrip().upper().startswith('CREATE TABLE'):
                continue
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                if owner != self.current:
                    sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('NEW',) if operation == 'INSERT' else ('OLD',)
                    condition = ' OR '.join('(source='+side+'.'+self.source_column+' AND generation='+side+'.'+self.generation_column+')' for side in sides)
                    name = owner+'_mapped_'+operation.lower()
                    yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                        f'WHEN EXISTS(SELECT 1 FROM {custody} WHERE {condition}) '
                        "BEGIN SELECT RAISE(ABORT,'Mapped document custody is retained'); END")
                name = owner+'_map_epoch_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                    f'BEGIN UPDATE {epoch} SET revision=revision+1 WHERE id=1; END')
        yield from invalidators(head, {self.name:self.job})
        name = head+'_preparation_input'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{self.job.state} '
            'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
            f'UPDATE {head} SET active=0 WHERE active=1; END')

    def validate(self, conn):
        from ..schema_contract import tokens as _tokens
        from ..entity_names import predecessor_definition as transform
        renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone()
        # Frozen table replacement drops triggers attached to the retired source
        # or dependency. Their selection is no longer used once native owns it;
        # retained maps and their custody guards still require exact definitions.
        retired = ({name for name, _ in invalidators(self.table('head'), {self.name:self.job})}
                   if self.native(conn) else set())
        expected = tuple(self.definitions())
        observed = schema_definitions(conn, [name for name, _ in expected])
        for name, sql in expected:
            if name not in observed and name in retired:
                continue
            if name not in observed or _tokens(observed[name]) != _tokens(transform(sql) if renamed else sql):
                raise MigrationError('Incompatible mapped publication object: '+name)
        if not conn.execute('SELECT 1 FROM '+self.table('epoch')+' WHERE id=1').fetchone():
            raise MigrationError('Mapped publication requires its custody revision')

    def validate_preparation(self, conn):
        from ..schema_contract import tokens as _tokens
        optional = {f'prepare_{self.job.name}_dependency_{owner}_{operation}'
            for owner in self.job.optional_dependencies for operation in ('insert', 'update', 'delete')}
        expected = tuple(self.job.definitions(conn))
        observed = schema_definitions(conn, [name for name, _ in expected])
        for name, sql in expected:
            if name not in observed and name in optional:
                continue
            if name not in observed or _tokens(observed[name]) != _tokens(sql):
                raise MigrationError('Incompatible preparation object: '+name)

    def validate_installed(self, conn):
        head = self.table('head')
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (head,)).fetchone():
            return
        self.validate(conn)
        if not self.native(conn):
            self.validate_preparation(conn)
        if conn.execute('SELECT 1 FROM '+head+' h LEFT JOIN '+self.table('generations')+
                ' g ON g.revision=h.revision LEFT JOIN '+self.table('stages')+
                ' s ON s.id=g.revision WHERE g.revision IS NULL OR s.id IS NULL OR s.complete<>1 '
                'OR s.input_revision<>g.input_revision LIMIT 1').fetchone():
            raise MigrationError('Mapped selection requires its completed stage')

    def install(self, conn):
        if conn.in_transaction:
            raise MigrationError('Mapped publication requires an idle connection')
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
        progress = self.worker.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.input_revision != progress.prepared_revision
                or conn.execute('SELECT 1 FROM '+self.job.changes+' LIMIT 1').fetchone()):
            raise MigrationError('Mapped publication requires reconciled preparation')
        return progress, conn.execute('SELECT revision FROM '+self.table('epoch')+' WHERE id=1').fetchone()[0]

    def stage(self, conn, identity):
        row = conn.execute('SELECT * FROM '+self.table('stages')+' WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise MigrationError('Mapped publication stage is missing')
        return Stage(*row)

    def step(self, conn, *, batch_size=128):
        if type(batch_size) is not int or not 1 <= batch_size <= 128:
            raise ValueError('Publication map batches require 1 to 128 links')
        self.install(conn)
        try:
            conn.execute('BEGIN IMMEDIATE')
            progress, epoch, *_ = self.proof(conn)
            row = conn.execute('SELECT * FROM '+self.table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
            if row is None or (row['input_revision'], row['custody_revision']) != (progress.input_revision, epoch):
                identity = conn.execute('INSERT INTO '+self.table('stages')+
                    '(input_revision,custody_revision,after_source,copied,complete) VALUES (?,?,NULL,0,0)',
                    (progress.input_revision, epoch)).lastrowid
                stage = self.stage(conn, identity)
            else:
                stage = Stage(*row)
            if not stage.complete:
                links = conn.execute('SELECT '+self.source_column+','+self.generation_column+' FROM '+self.current+
                    (' WHERE '+self.source_column+'>?' if stage.after_source is not None else '')+
                    ' ORDER BY '+self.source_column+' LIMIT ?',
                    ((stage.after_source,) if stage.after_source is not None else ())+(batch_size+1,)).fetchall()
                batch = links[:batch_size]
                for source, generation in batch:
                    conn.execute('INSERT INTO '+self.table('custody')+' VALUES (?,?) ON CONFLICT DO NOTHING', (source, generation))
                    conn.execute('INSERT INTO '+self.table('map')+' VALUES (?,?,?)', (stage.id, source, generation))
                conn.execute('UPDATE '+self.table('stages')+' SET after_source=?,copied=?,complete=? WHERE id=?',
                    (batch[-1][0] if batch else stage.after_source, stage.copied+len(batch), int(len(links)<=batch_size), stage.id))
                stage = self.stage(conn, stage.id)
            conn.commit()
            return stage
        except BaseException:
            conn.rollback()
            raise

    def capture(self, conn, stage, source):
        return conn.execute('SELECT g.* FROM '+self.table('map')+' m JOIN '+self.header+
            ' g ON g.'+self.source_column+'=m.source AND g.'+self.generation_column+'=m.generation WHERE m.stage=? AND m.source=?',
            (stage, source)).fetchone()

    def verify(self, conn, identity):
        if conn.in_transaction:
            raise MigrationError('Mapped verification requires an idle connection')
        try:
            conn.execute('BEGIN')
            self.job._version(conn)
            self.job.validate(conn)
            self.validate(conn)
            proof = self.proof(conn)
            stage = self.stage(conn, identity)
            if not stage.complete or (stage.input_revision, stage.custody_revision) != (proof[0].input_revision, proof[1]):
                raise MigrationError('Publication map is incomplete or changed; stage the reconciled inputs again')
        finally:
            conn.rollback()
        # Completed stages, their links and captured members are immutable. The
        # source/reference proof still fences writes during the entire audit.
        after, count = None, 0
        while True:
            batch = conn.execute('SELECT h.'+self.source_column+',h.'+self.generation_column+',m.generation FROM '+self.current+
                ' h LEFT JOIN '+self.table('map')+' m ON m.stage=? AND m.source=h.'+self.source_column+
                (' WHERE h.'+self.source_column+'>?' if after is not None else '')+
                ' ORDER BY h.'+self.source_column+' LIMIT 128',
                (identity,)+((after,) if after is not None else ())).fetchall()
            if not batch:
                break
            for source, generation, mapped in batch:
                if mapped != generation:
                    raise MigrationError('Publication map differs from its prepared heads')
                header = self.capture(conn, identity, source)
                if header is None:
                    raise MigrationError('Mapped original is missing')
                self.verify_document(conn, header)
            count += len(batch)
            after = batch[-1][0]
        if count != stage.copied or conn.execute('SELECT count(*) FROM '+self.table('map')+
                ' WHERE stage=?', (identity,)).fetchone()[0] != count:
            raise MigrationError('Publication map contains missing or extra documents')
        # Matching the prepared heads alone cannot prove that a whole source
        # document was not omitted before staging. Every mapped document was
        # checked against its installed source above; compare cardinality too.
        if len(self.job.keys) != 1:
            raise MigrationError('Mapped publication requires one installed source owner')
        source = next(iter(self.job.keys))
        predicate = self.job._filter(source)
        if conn.execute('SELECT count(*) FROM main.'+source+
                (' WHERE '+predicate if predicate else '')).fetchone()[0] != count:
            raise MigrationError('Publication map omits installed source documents')
        try:
            conn.execute('BEGIN')
            if self.proof(conn) != proof or self.stage(conn, identity) != stage:
                raise MigrationError('Mapped inputs changed during verification; reconcile again')
            return proof, stage
        finally:
            conn.rollback()

    def publish(self, conn, *, stage, clock=time.time):
        if type(stage) is not int or stage <= 0:
            raise ValueError('Mapped publication requires a positive stage identity')
        self.install(conn)
        captured = self.verify(conn, stage)
        try:
            conn.execute('BEGIN IMMEDIATE')
            self.job._version(conn)
            self.job.validate(conn)
            self.validate(conn)
            if (self.proof(conn), self.stage(conn, stage)) != captured:
                raise MigrationError('Mapped inputs changed after verification; reconcile again')
            conn.execute('INSERT INTO '+self.table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING',
                (stage, captured[1].input_revision, clock()))
            conn.execute('INSERT INTO '+self.table('head')+' VALUES (?,?,1) ON CONFLICT(name) '
                'DO UPDATE SET revision=excluded.revision,active=1', (self.name, stage))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def selected(self, conn):
        if self.native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return None
        for owner in self.job.optional_dependencies:
            if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone():
                for operation in ('insert', 'update', 'delete'):
                    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',
                            (f'prepare_{self.job.name}_dependency_{owner}_{operation}',)).fetchone():
                        return None
        row = conn.execute('SELECT revision FROM '+self.table('head')+' WHERE name=? AND active=1', (self.name,)).fetchone()
        return row[0] if row else None

    def value(self, conn, stage, source, default=None):
        header = self.capture(conn, stage, source)
        return self.read_original(conn, header) if header is not None else default
