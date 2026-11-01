"""Audit historical field contracts separately from guarding future writes.

Each installed physical owner has its own capture, finite row batches and proof.
Installing the future-write guards never scans, copies or replaces that owner.
Invalid history remains explicit audit evidence and blocks verification, rather
than blocking unrelated regular schema advancement.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from ..migrations.v104_domain_contracts import CONTRACTS
from .rows import Job


MAX_ROW_BYTES = 128 * 1024


@dataclass(frozen=True)
class ContractJob(Job):
    contract_owner: str = ''
    contract_fields: tuple = ()

    def definitions(self, conn=None):
        yield from super().definitions(conn)
        for column, expression in self.contract_fields:
            guarded = re.sub(r'\b('+'|'.join(re.escape(name) for name,_ in self.contract_fields)+r')\b',
                lambda match:'NEW.'+match[0],expression)
            for operation in ('INSERT','UPDATE'):
                name = f'prepare_{self.name}_{column}_guard_{operation.lower()}'
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{self.contract_owner} '
                    f'WHEN NOT ({guarded}) BEGIN SELECT RAISE(ABORT,'
                    f"'Invalid future {self.contract_owner}.{column}; historical audit is separate'); END")


def table(job, suffix):
    return 'prepared_' + job.name + '_' + suffix


def captured(column):
    # Preserve SQLite storage type as well as value, including BLOBs and
    # infinities that ordinary JSON cannot represent. Clip transfer size only;
    # the complete encoded row must fit the budget or the batch fails intact.
    value = (f"CASE WHEN typeof({column})='blob' THEN hex(substr({column},1,{MAX_ROW_BYTES+1})) "
        f"WHEN typeof({column})='real' AND abs({column})>1.7976931348623157e308 THEN "
        f"CASE WHEN {column}>0 THEN '+Infinity' ELSE '-Infinity' END "
        f"WHEN typeof({column})='text' THEN CAST(substr(CAST({column} AS BLOB),1,{MAX_ROW_BYTES+1}) AS TEXT) ELSE {column} END")
    # SQLite's JSON number formatter rounds REAL values. Keep DBAPI's original
    # numeric value separate from its type and let canonical() encode it.
    return (f'typeof({column}) AS _captured_type_{column}',
            f'{value} AS _captured_value_{column}')


def job(conn, owner):
    if owner not in CONTRACTS:
        raise ValueError('Unsupported domain-contract owner')
    return contract_job(conn,owner,CONTRACTS[owner])


def contract_job(conn, owner, fields, *, signature_tag='typed-sqlite-2', minimum=79):
    """Build a bounded audit for a finite, caller-defined installed contract."""
    info = list(conn.execute('PRAGMA main.table_info(' + owner + ')'))
    if (not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone()
            or not fields.keys() <= {row[1] for row in info}):
        raise MigrationError('Domain-contract owner is not available on this installed layout: ' + owner)
    keys = tuple(row[1] for row in sorted(info,key=lambda row:row[5]) if row[5])
    if not keys:
        raise MigrationError('Domain audit requires an installed durable key: ' + owner)
    signature = hashlib.sha256((','.join(keys) + ';'+signature_tag).encode()).hexdigest()[:8]
    name = 'contract_' + owner + '_' + signature
    originals, rows = 'prepared_' + name + '_originals', 'prepared_' + name + '_rows'
    destinations = (
        (originals, f'''CREATE TABLE {originals} (
            source_key TEXT NOT NULL CHECK(json_valid(source_key)),source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
            snapshot TEXT NOT NULL CHECK(json_valid(snapshot)),PRIMARY KEY(source_key,source_digest)) WITHOUT ROWID'''),
        (rows, f'''CREATE TABLE {rows} (
            source_key TEXT PRIMARY KEY CHECK(json_valid(source_key)),source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
            violation_count INTEGER NOT NULL CHECK(violation_count>=0),violations TEXT NOT NULL CHECK(json_valid(violations)),
            FOREIGN KEY(source_key,source_digest) REFERENCES {originals}(source_key,source_digest)) WITHOUT ROWID'''),
        (rows + '_invalid', f'CREATE INDEX {rows}_invalid ON {rows}(source_key) WHERE violation_count>0'),
    )
    def copy(conn, source_owner, source_key, row):
        captured_fields = {column: {'type': row['_captured_type_' + column],
                           'value': row['_captured_value_' + column]} for column in fields}
        raw = canonical({'key':json.loads(source_key),'fields':captured_fields})
        if len(raw) > MAX_ROW_BYTES:
            raise ValueError('Domain audit row exceeds its preparation byte budget')
        digest = hashlib.sha256(raw).hexdigest()
        violations = [column for column in fields if row['_valid_' + column] != 1]
        conn.execute(f'INSERT INTO {originals} VALUES (?,?,?) ON CONFLICT(source_key,source_digest) DO NOTHING', (source_key,digest,raw.decode()))
        conn.execute(f'INSERT INTO {rows} VALUES (?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET '
            'source_digest=excluded.source_digest,violation_count=excluded.violation_count,violations=excluded.violations',
            (source_key,digest,len(violations),json.dumps(violations,separators=(',',':'))))
    def delete(conn, source_owner, source_key):
        conn.execute(f'DELETE FROM {rows} WHERE source_key=?',(source_key,))
    projections = (*keys, *(value for column, expression in fields.items()
        for value in (*captured(column),'(' + expression + ') AS _valid_' + column)))
    return ContractJob(name,minimum,107,{owner:keys},destinations,copy,delete,
        projections={owner:projections},batch_limit=32,refresh_highwater=False,contract_owner=owner,
        contract_fields=tuple(fields.items()))


def original(conn, job, key, digest):
    row = conn.execute(f'SELECT snapshot FROM {table(job,"originals")} WHERE source_key=? AND source_digest=?',(key,digest)).fetchone()
    if row is None or hashlib.sha256(row[0].encode()).hexdigest() != digest:
        raise MigrationError('Domain audit original differs from its captured bytes')
    return json.loads(row[0])


def verify(conn, job):
    """Check the captured proof and indexed blockers without rescanning history."""
    if conn.in_transaction:
        raise MigrationError('Domain audit verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        job._version(conn)
        job.validate(conn)
        progress = job.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision
                or conn.execute(f'SELECT 1 FROM {job.changes} LIMIT 1').fetchone()):
            raise MigrationError('Domain audit requires its reconciled preparation revision')
        blocked = conn.execute(f'SELECT source_key,violations FROM {table(job,"rows")} WHERE violation_count>0 ORDER BY source_key LIMIT 1').fetchone()
        if blocked:
            raise MigrationError(f'Invalid historical {job.contract_owner}: key={blocked[0]}, fields={blocked[1]}; original values retained')
        return progress
    finally:
        conn.rollback()


def main(argv=None):
    import argparse
    from contextlib import closing
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('owner',choices=tuple(CONTRACTS))
    parser.add_argument('database',type=Path)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=32)
    parser.add_argument('--retry',action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 512 or not 1 <= args.batch_size <= 512:
        parser.error('--batches and --batch-size must be from 1 through 512')
    try:
        mode = 'ro' if args.status or args.verify else 'rw'
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri() + '?mode=' + mode,uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            selected = job(conn,args.owner)
            if args.verify:
                result = verify(conn,selected)
            elif args.status:
                result = selected.progress(conn) if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(selected.state,)).fetchone() else None
            else:
                result = selected.install(conn)
                for _ in range(args.batches):
                    result = selected.step(conn,batch_size=args.batch_size,retry=args.retry)
                    if result.status == 'prepared':
                        break
            print(json.dumps(asdict(result) if result else {'status':'uninitialized'},separators=(',',':')))
    except (MigrationError,sqlite3.Error,ValueError) as exc:
        parser.exit(1,f'Domain-contract preparation failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
