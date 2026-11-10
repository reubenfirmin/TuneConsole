"""Prepare calendar lookup indexes without indexing a populated serving owner.

The lookup owners and their indexes start empty. Keyset batches fill them while
ordinary source writes maintain their corresponding lookup rows atomically.
Only an explicitly reconciled revision can select them for runtime queries.
"""
from dataclasses import dataclass
import sqlite3
import time

from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from .rows import Job
from . import bounded


EVENTS = 'prepared_calendar_event_lookup'
OBSERVATIONS = 'prepared_calendar_observation_lookup'
SELECTION = 'published_calendar_lookup'
CUSTODY = 'prepared_calendar_lookup_custody'
OWNERS = {'play_events':EVENTS,'listening_observations':OBSERVATIONS}
FIELDS = {
    'play_events':('id','account_id','unresolved_subject_key','played_at'),
    'listening_observations':('id','account_id','unresolved_subject_key','source_day','timezone'),
}
PREDICATES = {'play_events':"{prefix}unresolved_subject_key IS NOT NULL",
    'listening_observations':"{prefix}recording_id IS NULL AND {prefix}precision='calendar_day'"}
MAX_ROW_BYTES = 128 * 1024


def put(owner):
    fields = FIELDS[owner]
    return ('INSERT INTO '+OWNERS[owner]+'('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') '
            'ON CONFLICT(id) DO UPDATE SET '+','.join(column+'=excluded.'+column for column in fields if column != 'id'))


def copy(conn, owner, key, row):
    if not row['_qualifies']:
        delete(conn,owner,key)
        return
    if len(canonical(row)) > MAX_ROW_BYTES:
        raise ValueError('Calendar index row exceeds its preparation byte budget')
    conn.execute(put(owner),tuple(row[column] for column in FIELDS[owner]))


def delete(conn, owner, key):
    import json
    conn.execute('DELETE FROM '+OWNERS[owner]+' WHERE id=?',json.loads(key))


@dataclass(frozen=True)
class IndexJob(Job):
    account_column: str = 'account_id'

    def definitions(self, conn=None):
        yield from super().definitions(conn)
        for owner,target in OWNERS.items():
            fields = FIELDS[owner]
            columns = ','.join(fields)
            values = ','.join('NEW.'+(self.account_column if column == 'account_id' else column) for column in fields)
            predicate = PREDICATES[owner].format(prefix='NEW.')
            for operation in ('INSERT','UPDATE','DELETE'):
                name = 'maintain_calendar_lookup_'+owner+'_'+operation.lower()
                remove = 'DELETE FROM '+target+' WHERE id=OLD.id;' if operation != 'INSERT' else ''
                insert = ('INSERT INTO '+target+'('+columns+') SELECT '+values+' WHERE '+predicate+
                    ' ON CONFLICT(id) DO UPDATE SET '+','.join(column+'=excluded.'+column for column in fields if column != 'id')+';') if operation != 'DELETE' else ''
                yield name, f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN {remove}{insert} END'

    def validate(self, conn):
        super().validate(conn)
        # Older prepared lookups remain a supported fallback until the explicit
        # worker installs custody and independently verifies them. Once custody
        # exists its write fences are required, including during normal startup.
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(CUSTODY,)).fetchone():
            from ..schema_contract import tokens as _tokens
            for name,sql in custody():
                row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
                if row is None or _tokens(row[0])!=_tokens(sql):
                    raise MigrationError('Incompatible calendar lookup custody: '+name)


DESTINATIONS = (
    (EVENTS,f'''CREATE TABLE {EVENTS} (
        id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,unresolved_subject_key TEXT NOT NULL,played_at REAL NOT NULL)'''),
    (EVENTS+'_scope',f'CREATE INDEX {EVENTS}_scope ON {EVENTS}(account_id,unresolved_subject_key,played_at)'),
    (OBSERVATIONS,f'''CREATE TABLE {OBSERVATIONS} (
        id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,unresolved_subject_key TEXT NOT NULL,source_day TEXT NOT NULL,timezone TEXT NOT NULL)'''),
    (OBSERVATIONS+'_scope',f'CREATE INDEX {OBSERVATIONS}_scope ON {OBSERVATIONS}(account_id,unresolved_subject_key,source_day,timezone)'),
    (SELECTION,f'CREATE TABLE {SELECTION} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0),published_at REAL NOT NULL)'),
)


def custody():
    yield CUSTODY, f'''CREATE TABLE {CUSTODY} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0),
        verified INTEGER NOT NULL CHECK(verified IN (0,1)))'''
    for owner in OWNERS.values():
        for operation in ('INSERT','UPDATE','DELETE'):
            name = owner+'_custody_'+operation.lower()
            yield name, ('CREATE TRIGGER '+name+' AFTER '+operation+' ON main.'+owner+' BEGIN '+
                'INSERT INTO '+CUSTODY+' VALUES (1,1,0) ON CONFLICT(id) DO UPDATE SET revision=revision+1; END')


def job(conn):
    columns = {row[1] for row in conn.execute('PRAGMA main.table_info(play_events)')}
    account = 'account_id' if 'account_id' in columns else 'identity_id'
    projections = {owner:tuple((account+' AS account_id') if column == 'account_id' else column for column in fields)+
        ('('+PREDICATES[owner].format(prefix='')+') AS _qualifies',) for owner,fields in FIELDS.items()}
    return IndexJob('calendar_indexes',100,107,{owner:('id',) for owner in OWNERS},DESTINATIONS,copy,delete,
                    projections=projections,batch_limit=32,refresh_highwater=False,account_column=account,
                    additive_destinations=tuple(custody()))


def selected(conn):
    return (conn.execute('SELECT 1 FROM main.sqlite_schema WHERE type=\'table\' AND name=?',(SELECTION,)).fetchone() is not None
            and conn.execute('SELECT 1 FROM main.sqlite_schema WHERE type=\'table\' AND name=?',(CUSTODY,)).fetchone() is not None
            and conn.execute(f'SELECT 1 FROM main.{CUSTODY} WHERE id=1 AND verified=1').fetchone() is not None
            and conn.execute(f'SELECT 1 FROM main.{SELECTION} WHERE id=1').fetchone() is not None)


def validate_installed(conn):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(SELECTION,)).fetchone():
        job(conn).validate(conn)


def proof(conn, selected_job, revision):
    progress = selected_job.progress(conn)
    if (progress is None or progress.status!='prepared' or progress.input_revision!=revision
            or progress.prepared_revision!=revision or conn.execute('SELECT 1 FROM '+selected_job.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Calendar index publication requires its reconciled preparation revision')
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(CUSTODY,)).fetchone():
        raise MigrationError('Calendar lookup requires explicit custody preparation before verification')
    current = conn.execute('SELECT revision FROM '+CUSTODY+' WHERE id=1').fetchone()
    return progress,current[0] if current else 0,conn.execute('PRAGMA main.schema_version').fetchone()[0]


def verify(conn, *, revision):
    """Compare every lookup value and coverage outside a writer reservation."""
    if conn.in_transaction:
        raise MigrationError('Calendar index publication requires an idle connection')
    selected_job = job(conn)
    try:
        conn.execute('BEGIN')
        selected_job._version(conn)
        selected_job.validate(conn)
        captured = proof(conn,selected_job,revision)
    finally:
        conn.rollback()
    for owner,target in OWNERS.items():
        after,count = None,0
        fields = FIELDS[owner]
        projection = ','.join(selected_job.account_column+' AS account_id' if field=='account_id' else field for field in fields)
        projection += ',('+PREDICATES[owner].format(prefix='')+') AS _qualifies'
        while True:
            try:
                conn.execute('BEGIN')
                if proof(conn,selected_job,revision)!=captured:
                    raise MigrationError('Calendar inputs changed during lookup verification')
                rows = bounded.rows(conn,'SELECT '+projection+' FROM main.'+owner+
                    (' WHERE id>?' if after is not None else '')+' ORDER BY id LIMIT 32',
                    (after,) if after is not None else (),byte_limit=MAX_ROW_BYTES,
                    message='Calendar lookup source exceeds its verification byte budget')
                for row in rows:
                    saved = conn.execute('SELECT * FROM '+target+' WHERE id=?',(row['id'],)).fetchone()
                    expected = tuple(row[field] for field in fields) if row['_qualifies'] else None
                    if (tuple(saved) if saved else None)!=expected:
                        raise MigrationError('Calendar lookup differs from its source: '+owner+' '+str(row['id']))
                    count += int(bool(row['_qualifies']))
                if not rows:
                    if conn.execute('SELECT count(*) FROM '+target).fetchone()[0]!=count:
                        raise MigrationError('Calendar lookup coverage differs from its source: '+owner)
                    break
                after = rows[-1]['id']
            finally:
                conn.rollback()
    if proof(conn,selected_job,revision)!=captured:
        raise MigrationError('Calendar inputs changed after lookup verification')
    return captured


def publish(conn, *, revision, clock=time.time):
    if type(revision) is not int or revision < 0:
        raise ValueError('Calendar index publication requires its nonnegative input revision')
    captured = verify(conn,revision=revision)
    selected_job = job(conn)
    try:
        conn.execute('BEGIN IMMEDIATE')
        selected_job.validate(conn)
        if proof(conn,selected_job,revision)!=captured:
            raise MigrationError('Calendar inputs changed after lookup verification')
        conn.execute(f'INSERT INTO {SELECTION} VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,published_at=excluded.published_at',
                     (revision,clock()))
        conn.execute('INSERT INTO '+CUSTODY+' VALUES (1,?,1) ON CONFLICT(id) DO UPDATE SET verified=1',(captured[1],))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--publish',type=int,metavar='REVISION')
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=32)
    parser.add_argument('--retry',action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 512 or not 1 <= args.batch_size <= 512:
        parser.error('--batches and --batch-size must be from 1 through 512')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode='+('ro' if args.status else 'rw'),uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            selected_job = job(conn)
            if args.status:
                result = selected_job.progress(conn) if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(selected_job.state,)).fetchone() else None
            elif args.publish is not None:
                publish(conn,revision=args.publish)
                result = selected_job.progress(conn)
            else:
                result = selected_job.install(conn)
                for _ in range(args.batches):
                    result = selected_job.step(conn,batch_size=args.batch_size,retry=args.retry)
                    if result.status == 'prepared':
                        break
            print(json.dumps({'progress':asdict(result) if result else None,'selected':selected(conn)},separators=(',',':')))
    except (MigrationError,sqlite3.Error,ValueError) as exc:
        parser.exit(1,f'Calendar index preparation failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
