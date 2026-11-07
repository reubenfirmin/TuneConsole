"""Select file projections together with their exact evidence retention roots."""
from dataclasses import dataclass
import hashlib
import json
import time

from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import ranked_state as preparation
from .ranked_verification import source_row, verify_document
from .mapped_publication import MappedPublication
from .selection import invalidators


@dataclass(frozen=True)
class Stage:
    id: int
    outputs_complete: int
    roots_copied: int
    complete: int


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='model_pair_publications'").fetchone() is not None


class RetainedPublication(MappedPublication):
    roots = preparation
    root_schema = preparation.home_preparation
    root_kind = 'comparison'
    root_namespace = 'model-pair'
    root_tag = 'pair'
    title = 'Comparison'

    @staticmethod
    def serialize_root(value):
        return canonical(value).decode()

    def definitions(self):
        yield from super().definitions()
        stages, mapping, published = (self.table(suffix) for suffix in ('root_stages', 'root_map', 'root_publications'))
        yield stages, f'''CREATE TABLE {stages} (
            stage INTEGER PRIMARY KEY REFERENCES {self.table('stages')}(id),after_owner TEXT,after_key TEXT,
            copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
        yield mapping, f'''CREATE TABLE {mapping} (
            stage INTEGER NOT NULL REFERENCES {stages}(stage),owner TEXT NOT NULL,source_key TEXT NOT NULL,
            source_digest TEXT NOT NULL,PRIMARY KEY(stage,owner,source_key),
            FOREIGN KEY(owner,source_key,source_digest) REFERENCES {self.roots.ORIGINALS}(owner,source_key,digest)) WITHOUT ROWID'''
        yield published, f'''CREATE TABLE {published} (
            stage INTEGER PRIMARY KEY REFERENCES {self.table('generations')}(revision),
            FOREIGN KEY(stage) REFERENCES {stages}(stage))'''
        for owner in (mapping, published, self.roots.ORIGINALS):
            for operation in ('UPDATE', 'DELETE'):
                name = owner+'_'+self.root_tag+'_retained_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                    f"BEGIN SELECT RAISE(ABORT,'{self.title} publication custody requires future cleanup'); END")
        for operation in ('UPDATE', 'DELETE'):
            name = stages+'_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{stages} WHEN OLD.complete=1 '
                f"BEGIN SELECT RAISE(ABORT,'Completed {self.title.lower()} root stages are retained'); END")
        name = mapping+'_insert'
        yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{mapping} '
            f'WHEN EXISTS(SELECT 1 FROM {stages} WHERE stage=NEW.stage AND complete=1) '
            f"BEGIN SELECT RAISE(ABORT,'Completed {self.title.lower()} root maps are retained'); END")
        for owner in (self.roots.ROOTS, self.roots.ORIGINALS):
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = owner+'_'+self.root_tag+'_epoch_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                    f'BEGIN UPDATE {self.table("epoch")} SET revision=revision+1 WHERE id=1; END')
        name = self.table('head')+'_root_input'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{self.roots.ROOT_JOB.state} '
            'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
            f'UPDATE {self.table("epoch")} SET revision=revision+1 WHERE id=1; '
            f'UPDATE {self.table("head")} SET active=0 WHERE active=1; END')
        yield from invalidators(self.table('head'), {'roots':self.roots.ROOT_JOB}, names={'roots':self.name})
        for owner in self.roots.ROOT_JOB.keys:
            for operation in ('UPDATE', 'DELETE'):
                name = owner+'_'+self.root_tag+'_pin_'+operation.lower()
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                    f"WHEN OLD.kind='{self.root_kind}' AND OLD.external_key GLOB 'preparation:{self.root_namespace}:*' "
                    f"BEGIN SELECT RAISE(ABORT,'Retained {self.title.lower()} pins require future cleanup'); END")

    def pin_key(self, stage, external_key):
        return 'preparation:'+self.root_namespace+':'+str(stage)+':'+hashlib.sha256(external_key.encode()).hexdigest()

    def pin(self, conn, stage, owner, key):
        """Copy existing FK-backed custody exactly, without resolving aliases again."""
        keys = self.roots.ROOT_JOB.keys[owner]
        values = json.loads(key)
        row = conn.execute('SELECT * FROM main.'+owner+' WHERE '+
            ' AND '.join(column+'=?' for column in keys), values).fetchone()
        if row is None:
            raise MigrationError(f'Installed {self.title.lower()} root changed before retention')
        retained_key = self.pin_key(stage, row['external_key'])
        def copy(table, source):
            fields = dict(source)
            fields['external_key'] = retained_key
            conn.execute('INSERT INTO main.'+table+'('+','.join(fields)+') VALUES ('+
                ','.join('?' for _ in fields)+') ON CONFLICT DO NOTHING', tuple(fields.values()))
        if owner not in ('group_evidence_owners', 'recommendation_generation_refs'):
            parent = conn.execute('SELECT * FROM group_evidence_owners WHERE kind=? AND external_key=?',
                (row['kind'], row['external_key'])).fetchone()
            if parent is None:
                raise MigrationError(f'Installed {self.title.lower()} root has no custody owner')
            copy('group_evidence_owners', parent)
        copy(owner, row)

    def install(self, conn):
        self.roots.ROOT_JOB.validate(conn)
        return super().install(conn)

    def proof(self, conn):
        job = self.roots.ROOT_JOB
        job._version(conn)
        job.validate(conn)
        progress = job.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision
                or conn.execute('SELECT 1 FROM '+job.changes+' LIMIT 1').fetchone()):
            raise MigrationError(self.title+' publication requires reconciled retention roots')
        return super().proof(conn)

    def root_stage(self, conn, identity):
        return conn.execute('SELECT * FROM '+self.table('root_stages')+' WHERE stage=?', (identity,)).fetchone()

    def step(self, conn, *, batch_size=8):
        if type(batch_size) is not int or not 1 <= batch_size <= 8:
            raise ValueError(self.title+' publication batches require 1 to 8 links')
        outputs = super().step(conn, batch_size=batch_size)
        if not outputs.complete:
            return Stage(outputs.id, 0, 0, 0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            progress, epoch = self.proof(conn)
            if (outputs.input_revision, outputs.custody_revision) != (progress.input_revision, epoch):
                raise MigrationError(self.title+' inputs changed before root capture; stage again')
            conn.execute('INSERT INTO '+self.table('root_stages')+' VALUES (?,NULL,NULL,0,0) ON CONFLICT DO NOTHING', (outputs.id,))
            current = self.root_stage(conn, outputs.id)
            if not current['complete']:
                after = (current['after_owner'], current['after_key']) if current['after_owner'] is not None else ()
                rows = conn.execute('SELECT owner,source_key,source_digest FROM '+self.roots.ROOTS+
                    (' WHERE (owner,source_key)>(?,?)' if after else '')+' ORDER BY owner,source_key LIMIT ?',
                    (*after, batch_size)).fetchall()
                conn.executemany('INSERT INTO '+self.table('root_map')+' VALUES (?,?,?,?)',
                    [(outputs.id, *row) for row in rows])
                for owner, key, _digest in rows:
                    self.pin(conn, outputs.id, owner, key)
                cursor = tuple(rows[-1][:2]) if rows else (current['after_owner'], current['after_key'])
                conn.execute('UPDATE '+self.table('root_stages')+' SET after_owner=?,after_key=?,copied=copied+?,complete=? WHERE stage=?',
                    (*cursor, len(rows), int(len(rows)<batch_size), outputs.id))
            captured = self.root_stage(conn, outputs.id)
            conn.commit()
            return Stage(outputs.id, 1, captured['copied'], captured['complete'])
        except BaseException:
            conn.rollback()
            raise

    def verify_roots(self, conn, identity):
        if conn.in_transaction:
            raise MigrationError(self.title+' root verification requires an idle connection')
        stage = self.root_stage(conn, identity)
        if stage is None or not stage['complete']:
            raise MigrationError(self.title+' publication requires a completed root map')
        after, count = (), 0
        while True:
            rows = conn.execute('SELECT owner,source_key,source_digest FROM '+self.table('root_map')+
                ' WHERE stage=?'+(' AND (owner,source_key)>(?,?)' if after else '')+
                ' ORDER BY owner,source_key LIMIT 8', (identity, *after)).fetchall()
            if not rows:
                break
            for owner, key, digest in rows:
                if owner not in self.root_schema.ROOT_KEYS:
                    raise MigrationError(self.title+' map contains an unknown root owner')
                original = conn.execute('SELECT source_snapshot FROM '+self.roots.ORIGINALS+
                    ' WHERE owner=? AND source_key=? AND digest=?', (owner, key, digest)).fetchone()
                source = self.roots.ROOT_JOB._read(conn, owner, key)
                if source is None or original is None:
                    raise MigrationError(self.title+' retained root or installed source is missing')
                value = dict(source)
                if sum(len(value[field]) for field in value if field in self.root_schema.TEXT_FIELDS
                       and value[field] is not None) > self.root_schema.ROOT_BYTES:
                    raise MigrationError(self.title+' root exceeds its preparation byte budget')
                for field in self.root_schema.TEXT_FIELDS:
                    if field in value and value[field] is not None:
                        value[field] = bytes(value[field]).decode('utf-8')
                expected = self.serialize_root(value)
                if original[0] != expected or hashlib.sha256(expected.encode()).hexdigest() != digest:
                    raise MigrationError(self.title+' root archive differs from its installed source')
                prepared = conn.execute('SELECT * FROM '+self.roots.ROOTS+' WHERE owner=? AND source_key=?', (owner, key)).fetchone()
                fields = (owner, key, digest, value['kind'], value['external_key'],
                    *(value.get(field) for field in self.root_schema.ROOT_COLUMNS))
                if prepared is None or tuple(prepared) != fields:
                    raise MigrationError(f'Prepared {self.title.lower()} root differs from its retained original')
                values = dict(value)
                values['external_key'] = self.pin_key(identity, value['external_key'])
                keys = self.roots.ROOT_JOB.keys[owner]
                pin = conn.execute('SELECT '+','.join(values)+' FROM main.'+owner+' WHERE '+
                    ' AND '.join(field+'=?' for field in keys), tuple(values[field] for field in keys)).fetchone()
                if pin is None or tuple(pin) != tuple(values.values()):
                    raise MigrationError(f'Retained {self.title.lower()} pin differs from its captured custody')
            count += len(rows)
            after = tuple(rows[-1][:2])
        if count != stage['copied'] or conn.execute('SELECT count(*) FROM '+self.roots.ROOTS).fetchone()[0] != count:
            raise MigrationError(self.title+' root map contains missing or extra roots')
        installed = sum(conn.execute('SELECT count(*) FROM main.'+owner+' WHERE '+self.roots.ROOT_JOB._filter(owner)).fetchone()[0]
            for owner in self.roots.ROOT_JOB.keys)
        if installed != count:
            raise MigrationError(self.title+' root map omits installed retention roots')
        return tuple(stage)

    def publish(self, conn, *, stage, clock=time.time):
        if type(stage) is not int or stage<=0:
            raise ValueError(self.title+' publication requires a positive stage identity')
        self.install(conn)
        captured = self.verify(conn, stage)
        roots = self.verify_roots(conn, stage)
        try:
            conn.execute('BEGIN IMMEDIATE')
            self.job._version(conn)
            self.job.validate(conn)
            self.validate(conn)
            if (self.proof(conn), self.stage(conn, stage)) != captured or tuple(self.root_stage(conn, stage)) != roots:
                raise MigrationError(self.title+' inputs changed during verification; reconcile again')
            conn.execute('INSERT INTO '+self.table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING',
                (stage, captured[1].input_revision, clock()))
            conn.execute('INSERT INTO '+self.table('root_publications')+' VALUES (?) ON CONFLICT DO NOTHING', (stage,))
            conn.execute('INSERT INTO '+self.table('head')+' VALUES (?,?,1) ON CONFLICT(name) '
                'DO UPDATE SET revision=excluded.revision,active=1', (self.name, stage))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def validate_installed(self, conn):
        super().validate_installed(conn)
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return
        if not self.native(conn):
            self.roots.ROOT_JOB.validate(conn)
        if conn.execute('SELECT 1 FROM '+self.table('head')+' h LEFT JOIN '+self.table('root_publications')+
            ' p ON p.stage=h.revision LEFT JOIN '+self.table('root_stages')+
            ' s ON s.stage=p.stage WHERE p.stage IS NULL OR s.complete IS NOT 1 LIMIT 1').fetchone():
            raise MigrationError(self.title+' selection requires its retained root map')


class PairPublication(RetainedPublication):
    """Keep the installed pair schema and pin namespace unchanged."""


OWNER = PairPublication('pairs', preparation.JOBS['pairs'], 'prepared_model_pairs_generations',
    'prepared_model_pairs_heads', native, verify_document, preparation.read_original,
    source_column='surface', generation_column='preparation_revision')


def repair(conn, surface):
    if conn.in_transaction:
        raise MigrationError('Comparison file recovery requires an idle connection')
    preparation.model_projections.address(surface)
    header, row = preparation.head(conn, surface), source_row(conn, surface)
    if header is None or row is None:
        raise MigrationError('Comparison file recovery requires its retained source')
    if hashlib.sha256(bytes(row['payload'])).hexdigest() != header['raw_digest'] or row['built_at'] != header['built_at']:
        raise MigrationError('Comparison source changed; reconcile before file recovery')
    plan = preparation.prepare(conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt comparison plan differs from its retained manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    from .publication_cli import main as run
    return run(OWNER, __doc__, argv, label='Comparison', repair=repair, batch_size=8,
               repair_flag='--repair-surface')


if __name__ == '__main__':
    raise SystemExit(main())
