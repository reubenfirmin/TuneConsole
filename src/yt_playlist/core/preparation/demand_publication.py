"""Stage verified Home requests and held choice identities without retiring writers."""
from dataclasses import asdict, dataclass
import json
import time

from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import bounded, recommendation_demand as preparation


PREFIX = 'published_demand_'
# Preparation owner, stable key, maximum number of rows in the finite domain.
OWNERS = {
    'prepared_demand_originals': (('surface',), 5),
    'prepared_listening_choice_windows': (('format',), 3),
    'prepared_album_menu_refresh': (('id',), 1),
    'prepared_album_menu_exclusions': (('position',), 100),
    'prepared_home_genre_requests': (('id',), 1),
    'prepared_home_genre_items': (('kind', 'position'), 200),
}


def table(name):
    return PREFIX+name.removeprefix('prepared_')


@dataclass(frozen=True)
class Stage:
    id: int
    proof: str
    after_owner: str | None
    after_key: str | None
    copied: int
    complete: int


def definitions():
    yield table('epoch'), f'CREATE TABLE {table("epoch")} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
    yield table('stages'), f'''CREATE TABLE {table('stages')} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,proof TEXT NOT NULL CHECK(json_valid(proof)),
        after_owner TEXT,after_key TEXT CHECK(after_key IS NULL OR json_valid(after_key)),
        copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
    for owner, sql in preparation.JOB.destinations:
        body = sql.split('(', 1)[1].rsplit(')', 1)[0]
        keys, _ = OWNERS[owner]
        if len(keys) == 1:
            key, = keys
            body = body.replace(key+' TEXT PRIMARY KEY', key+' TEXT NOT NULL')
            body = body.replace(key+' INTEGER PRIMARY KEY', key+' INTEGER NOT NULL')
        else:
            body = body.replace('PRIMARY KEY('+','.join(keys)+'),', '')
        body = body.replace('REFERENCES prepared_album_menu_refresh(id)', '')
        body = body.replace('REFERENCES prepared_home_genre_requests(id)', '')
        yield table(owner), (f'CREATE TABLE {table(owner)} (stage INTEGER NOT NULL REFERENCES {table("stages")}(id),'
            +body+',PRIMARY KEY(stage,'+','.join(keys)+')) WITHOUT ROWID')
    yield table('head'), f'''CREATE TABLE {table('head')} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {table('stages')}(id),
        active INTEGER NOT NULL CHECK(active IN (0,1)),published_at REAL NOT NULL)'''
    for owner in (*map(table, OWNERS), table('stages')):
        for operation in ('UPDATE', 'DELETE'):
            name = owner+'_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                +('WHEN OLD.complete=1 ' if owner == table('stages') else '')
                +"BEGIN SELECT RAISE(ABORT,'Demand publication requires future cleanup'); END")
    for owner in OWNERS:
        name = table(owner)+'_insert'
        yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{table(owner)} '
            f'WHEN EXISTS(SELECT 1 FROM {table("stages")} WHERE id=NEW.stage AND complete=1) '
            "BEGIN SELECT RAISE(ABORT,'Completed demand stage is retained'); END")
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = table(owner)+'_prepared_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN '
                f'UPDATE {table("epoch")} SET revision=revision+1 WHERE id=1; '
                f'UPDATE {table("head")} SET active=0 WHERE active=1; END')
    name = table('input_changed')
    yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{preparation.JOB.state} '
        'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
        f'UPDATE {table("head")} SET active=0 WHERE active=1; END')


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='listening_choice_windows' AND type='table'").fetchone() is not None


def validate(conn):
    from ..schema_contract import tokens as _tokens
    from ..entity_names import predecessor_definition as transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
            raise MigrationError('Incompatible demand publication object: '+name)
    if conn.execute('SELECT 1 FROM '+table('epoch')+' WHERE id=1').fetchone() is None:
        raise MigrationError('Demand publication is missing its custody revision')


def validate_installed(conn):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone() is None:
        return
    validate(conn)
    if not native(conn):
        preparation.JOB.validate(conn)
    if conn.execute('SELECT 1 FROM '+table('head')+' h LEFT JOIN '+table('stages')+
        ' s ON s.id=h.revision WHERE s.id IS NULL OR s.complete<>1 LIMIT 1').fetchone():
        raise MigrationError('Demand selection requires a completed stage')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Demand publication requires an idle connection')
    def installed():
        preparation.JOB._version(conn)
        preparation.JOB.validate(conn)
        found = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone()
        if found:
            validate(conn)
        return found
    try:
        conn.execute('BEGIN')
        found = installed()
        conn.rollback()
        if found:
            return
        conn.execute('BEGIN IMMEDIATE')
        if not installed():
            for _, sql in definitions():
                conn.execute(sql)
            conn.execute('INSERT INTO '+table('epoch')+' VALUES (1,0)')
        conn.commit()
    finally:
        conn.rollback()


def proof(conn):
    progress = preparation.JOB.progress(conn)
    if (progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision
            or conn.execute('SELECT 1 FROM '+preparation.JOB.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Demand publication requires reconciled preparation')
    epoch = conn.execute('SELECT revision FROM '+table('epoch')+' WHERE id=1').fetchone()[0]
    return canonical([asdict(progress), epoch]).decode()


def stage(conn, identity=None):
    row = conn.execute('SELECT * FROM '+table('stages')+
        (' ORDER BY id DESC LIMIT 1' if identity is None else ' WHERE id=?'), () if identity is None else (identity,)).fetchone()
    if row is None and identity is not None:
        raise MigrationError('Demand publication stage is missing')
    return Stage(*row) if row else None


def rows(conn, owner, *, identity=None, after=None, size=None):
    keys, maximum = OWNERS[owner]
    predicates, parameters = [], []
    if identity is not None:
        predicates.append('stage=?')
        parameters.append(identity)
    if after is not None:
        predicates.append('('+','.join(keys)+')>('+','.join('?' for _ in keys)+')')
        parameters.extend(json.loads(after))
    query = 'SELECT * FROM main.'+(owner if identity is None else table(owner))
    query += (' WHERE '+' AND '.join(predicates)) if predicates else ''
    query += ' ORDER BY '+','.join(keys)+' LIMIT ?'
    result = bounded.rows(conn, query, (*parameters, size or maximum+1),
        byte_limit=6*preparation.MAX_BYTES+4096, message='Demand publication exceeds its row byte budget')
    if size is None and len(result)>maximum:
        raise MigrationError('Demand publication exceeds its finite member limit')
    return result


def batch(conn, current, size):
    result = []
    for owner in sorted(OWNERS):
        if current.after_owner is not None and owner<current.after_owner:
            continue
        after = current.after_key if owner == current.after_owner else None
        result.extend((owner, row) for row in rows(conn, owner, after=after, size=size-len(result)))
        if len(result) == size:
            break
    return result


def step(conn, *, batch_size=16):
    if type(batch_size) is not int or not 1<=batch_size<=16:
        raise ValueError('Demand publication requires 1 to 16 rows per batch')
    install(conn)
    try:
        conn.execute('BEGIN')
        captured, previous = proof(conn), stage(conn)
        current = previous if previous and previous.proof == captured else Stage(0, captured, None, None, 0, 0)
        if current.complete:
            return current
        work = batch(conn, current, batch_size+1)
        more, work = len(work)>batch_size, work[:batch_size]
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        if proof(conn) != captured or stage(conn) != previous:
            raise MigrationError('Demand changed before staging; reconcile again')
        if not current.id:
            identity = conn.execute('INSERT INTO '+table('stages')+'(proof,copied,complete) VALUES (?,0,0)', (captured,)).lastrowid
            current = stage(conn, identity)
        for owner, row in work:
            conn.execute('INSERT INTO '+table(owner)+'(stage,'+','.join(row)+') VALUES ('+
                ','.join('?' for _ in range(len(row)+1))+')', (current.id, *row.values()))
        owner, key = current.after_owner, current.after_key
        if work:
            owner, row = work[-1]
            key = canonical([row[field] for field in OWNERS[owner][0]]).decode()
        conn.execute('UPDATE '+table('stages')+' SET after_owner=?,after_key=?,copied=?,complete=? WHERE id=?',
            (owner, key, current.copied+len(work), int(not more), current.id))
        result = stage(conn, current.id)
        conn.commit()
        return result
    finally:
        conn.rollback()


def verify(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Demand verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        validate_installed(conn)
        captured, current = proof(conn), stage(conn, identity)
        if not current.complete or current.proof != captured:
            raise MigrationError('Demand stage is incomplete or changed')
        expected = {owner:[] for owner in OWNERS}
        for surface in preparation.SURFACES:
            source = preparation.JOB._read(conn, 'rec_proposals', json.dumps([surface], separators=(',', ':')))
            if source is not None:
                for owner, values in preparation.describe(conn, source).items():
                    expected[owner].extend(values)
        count = 0
        for owner, values in expected.items():
            prepared = rows(conn, owner)
            published = rows(conn, owner, identity=identity)
            if prepared:
                columns = tuple(prepared[0])
                values.sort(key=lambda value: tuple(value[columns.index(key)] for key in OWNERS[owner][0]))
            if [tuple(row.values()) for row in prepared] != values:
                raise MigrationError('Prepared demand differs from its installed source or references')
            if [{key:value for key,value in row.items() if key!='stage'} for row in published] != prepared:
                raise MigrationError('Demand publication differs from its verified preparation')
            count += len(prepared)
        if current.copied != count:
            raise MigrationError('Demand stage has missing or extra rows')
        return captured, current
    finally:
        conn.rollback()


def publish(conn, *, identity, clock=time.time):
    if type(identity) is not int or identity<=0:
        raise ValueError('Demand publication requires a positive stage identity')
    install(conn)
    captured = verify(conn, identity)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if (proof(conn), stage(conn, identity)) != captured:
            raise MigrationError('Demand changed after verification; reconcile again')
        conn.execute('INSERT INTO '+table('head')+' VALUES (1,?,1,?) '
            'ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1,published_at=excluded.published_at', (identity, clock()))
        conn.commit()
    finally:
        conn.rollback()


def selected(conn):
    if native(conn) or conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone() is None:
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


def read(conn, surface, identity):
    """Read only immutable typed members of a pinned request generation."""
    if surface not in preparation.SURFACES:
        raise ValueError('Unknown recommendation demand owner')
    def members(owner, predicate='', parameters=(), order=''):
        return conn.execute('SELECT * FROM '+table('prepared_'+owner)+' WHERE stage=?'+predicate+order,
                            (identity, *parameters))
    if surface.startswith('choice_window:'):
        row = members('listening_choice_windows', ' AND format=?', (surface.split(':',1)[1],)).fetchone()
        return {'opportunity_id':row['opportunity_key'], 'window':row['window_text']} if row else None
    if surface == 'album_menu_refresh':
        row = members('album_menu_refresh').fetchone()
        return ({'epoch':row['epoch'], 'exclude':[item['browse_id'] for item in members('album_menu_exclusions', order=' ORDER BY position')]}
                if row and row['epoch'] is not None else {} if row else None)
    row = members('home_genre_requests').fetchone()
    if row is None:
        return None
    result = {'epoch':row['epoch']} if row['epoch'] is not None else {}
    for field, flag, kind in (('include_genres','has_includes','include'), ('avoid_genres','has_avoids','avoid')):
        if row[flag]:
            result[field] = [item['raw_key'] for item in members('home_genre_items', ' AND kind=?', (kind,), ' ORDER BY position')]
    for field in ('held_by_vibe','suspended_by_vibe'):
        if row[field] is not None:
            result[field] = bool(row[field])
    return result


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--publish-stage', type=int)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=16)
    args = parser.parse_args(argv)
    if not 1<=args.batches<=128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                publish(conn, identity=args.publish_stage)
                result = {'selected':selected(conn)}
            else:
                for _ in range(args.batches):
                    current = step(conn, batch_size=args.batch_size)
                    if current.complete:
                        break
                result = asdict(current)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'Demand publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
