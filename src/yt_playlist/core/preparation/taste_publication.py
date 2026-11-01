"""Publish verified taste contributions without replacing installed writable owners."""
from dataclasses import asdict, dataclass
import json
import time

from .. import taste_axes
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import bounded, taste_axes as preparation


PREFIX = 'published_taste_'
FIELDS = ('owner','source_key','raw_axis','axis_key','source_digest',*preparation.VALUES)
AXIS_FIELDS = ('kind','genre_key','family_key','axis_value','resolution_reason')
MAX_BYTES = 128*1024


def table(name):
    return PREFIX+name


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
    body = dict(preparation.JOB.destinations)['prepared_taste_rows'].split('(',1)[1].rsplit(')',1)[0]
    body = body.replace(' REFERENCES prepared_taste_axis_keys(axis_key)','')
    body = body.replace('PRIMARY KEY(owner,source_key),','PRIMARY KEY(stage,owner,source_key),')
    body = body.replace('FOREIGN KEY(owner,source_key,source_digest) REFERENCES prepared_taste_originals(owner,source_key,digest)',
        "CHECK(kind IN ('genre','family','other','unresolved_genre'))")
    yield table('rows'), (f'CREATE TABLE {table("rows")} (stage INTEGER NOT NULL REFERENCES {table("stages")}(id),'
        'source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),kind TEXT NOT NULL,genre_key TEXT,family_key TEXT,axis_value TEXT,resolution_reason TEXT,'+body+') WITHOUT ROWID')
    yield table('axis_index'), f'CREATE INDEX {table("axis_index")} ON {table("rows")}(stage,owner,axis_key,source_key)'
    yield table('head'), f'''CREATE TABLE {table('head')} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {table('stages')}(id),
        active INTEGER NOT NULL CHECK(active IN (0,1)),published_at REAL NOT NULL)'''
    for owner in (table('rows'),table('stages'),'prepared_taste_originals','prepared_taste_axis_keys'):
        for operation in ('UPDATE','DELETE'):
            name = table(owner+'_'+operation.lower())
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                +('WHEN OLD.complete=1 ' if owner==table('stages') else '')
                +"BEGIN SELECT RAISE(ABORT,'Retained taste publication requires future cleanup'); END")
    name = table('rows_insert')
    yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{table("rows")} '
        f'WHEN EXISTS(SELECT 1 FROM {table("stages")} WHERE id=NEW.stage AND complete=1) '
        "BEGIN SELECT RAISE(ABORT,'Completed taste publication is retained'); END")
    for operation in ('INSERT','UPDATE','DELETE'):
        name = table('prepared_'+operation.lower())
        yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.prepared_taste_rows BEGIN '
            f'UPDATE {table("epoch")} SET revision=revision+1 WHERE id=1; '
            f'UPDATE {table("head")} SET active=0 WHERE active=1; END')
    name = table('input_changed')
    yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{preparation.JOB.state} '
        'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
        f'UPDATE {table("head")} SET active=0 WHERE active=1; END')


def validate(conn):
    from ..migrations.current import _tokens
    from ..migrations.v103_entity_names import transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for name,sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0])!=_tokens(transform(sql) if renamed else sql):
            raise MigrationError('Incompatible taste publication object: '+name)
    if conn.execute('SELECT 1 FROM '+table('epoch')+' WHERE id=1').fetchone() is None:
        raise MigrationError('Taste publication requires its custody revision')


def validate_installed(conn):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone() is None:
        return
    validate(conn)
    if not taste_axes.available(conn):
        preparation.JOB.validate(conn)
    if conn.execute('SELECT 1 FROM '+table('head')+' h LEFT JOIN '+table('stages')+
        ' s ON s.id=h.revision WHERE s.id IS NULL OR s.complete<>1 LIMIT 1').fetchone():
        raise MigrationError('Taste selection requires a completed stage')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Taste publication requires an idle connection')
    def installed():
        preparation.JOB._version(conn)
        preparation.JOB.validate(conn)
        found = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone()
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
            for _,sql in definitions():
                conn.execute(sql)
            conn.execute('INSERT INTO '+table('epoch')+' VALUES (1,0)')
        conn.commit()
    finally:
        conn.rollback()


def proof(conn):
    progress = preparation.JOB.progress(conn)
    if (progress is None or progress.status!='prepared' or progress.prepared_revision!=progress.input_revision
            or conn.execute('SELECT 1 FROM '+preparation.JOB.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Taste publication requires reconciled preparation')
    return canonical([asdict(progress),conn.execute('SELECT revision FROM '+table('epoch')+' WHERE id=1').fetchone()[0]]).decode()


def stage(conn,identity=None):
    row = conn.execute('SELECT * FROM '+table('stages')+
        (' ORDER BY id DESC LIMIT 1' if identity is None else ' WHERE id=?'),() if identity is None else (identity,)).fetchone()
    if row is None and identity is not None:
        raise MigrationError('Taste stage is missing')
    return Stage(*row) if row else None


def batch(conn,after_owner,after_key,size):
    query = ('SELECT p.*,o.source_snapshot,a.kind,a.genre_key,a.family_key,a.value AS axis_value,a.resolution_reason '
        'FROM prepared_taste_rows p JOIN prepared_taste_originals o ON '
        'o.owner=p.owner AND o.source_key=p.source_key AND o.digest=p.source_digest '
        'JOIN prepared_taste_axis_keys a ON a.axis_key=p.axis_key')
    parameters = ()
    if after_owner is not None:
        query += ' WHERE (p.owner,p.source_key)>(?,?)'
        parameters = (after_owner,after_key)
    return bounded.rows(conn,query+' ORDER BY p.owner,p.source_key LIMIT ?',(*parameters,size),
        byte_limit=MAX_BYTES,message='Taste publication exceeds its row byte budget')


def step(conn,*,batch_size=16):
    if type(batch_size) is not int or not 1<=batch_size<=16:
        raise ValueError('Taste publication requires 1 to 16 rows per batch')
    install(conn)
    try:
        conn.execute('BEGIN')
        captured,previous = proof(conn),stage(conn)
        current = previous if previous and previous.proof==captured else Stage(0,captured,None,None,0,0)
        if current.complete:
            return current
        work = batch(conn,current.after_owner,current.after_key,batch_size+1)
        more,work = len(work)>batch_size,work[:batch_size]
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        if proof(conn)!=captured or stage(conn)!=previous:
            raise MigrationError('Taste changed before staging; reconcile again')
        if not current.id:
            identity = conn.execute('INSERT INTO '+table('stages')+'(proof,copied,complete) VALUES (?,0,0)',(captured,)).lastrowid
            current = stage(conn,identity)
        for row in work:
            conn.execute('INSERT INTO '+table('rows')+'(stage,'+','.join(row)+') VALUES ('+
                ','.join('?' for _ in range(len(row)+1))+')',(current.id,*row.values()))
        conn.execute('UPDATE '+table('stages')+' SET after_owner=?,after_key=?,copied=?,complete=? WHERE id=?',
            (work[-1]['owner'] if work else current.after_owner,work[-1]['source_key'] if work else current.after_key,
             current.copied+len(work),int(not more),current.id))
        result = stage(conn,current.id)
        conn.commit()
        return result
    finally:
        conn.rollback()


def verify(conn,identity):
    if conn.in_transaction:
        raise MigrationError('Taste verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        validate_installed(conn)
        captured,current = proof(conn),stage(conn,identity)
        if not current.complete or current.proof!=captured:
            raise MigrationError('Taste stage is incomplete or changed')
    finally:
        conn.rollback()
    after_owner,after_key,count = None,None,0
    while True:
        try:
            conn.execute('BEGIN')
            if proof(conn)!=captured:
                raise MigrationError('Taste changed during verification; reconcile again')
            work = batch(conn,after_owner,after_key,16)
            for row in work:
                source = preparation.JOB._read(conn,row['owner'],row['source_key'])
                if source is None:
                    raise MigrationError('Taste publication has no installed source')
                expected,axis,snapshot = preparation.describe(conn,row['owner'],row['source_key'],dict(source))
                published = conn.execute('SELECT * FROM '+table('rows')+' WHERE stage=? AND owner=? AND source_key=?',
                    (identity,row['owner'],row['source_key'])).fetchone()
                if (tuple(row[field] for field in FIELDS)!=expected or tuple(row[field] for field in AXIS_FIELDS)!=axis
                        or row['source_snapshot']!=snapshot or published is None
                        or {key:published[key] for key in row}!=row):
                    raise MigrationError('Taste publication differs from its source, references or preparation')
            if not work:
                break
            after_owner,after_key = work[-1]['owner'],work[-1]['source_key']
            count += len(work)
        finally:
            conn.rollback()
    try:
        conn.execute('BEGIN')
        if (proof(conn),stage(conn,identity))!=(captured,current):
            raise MigrationError('Taste changed during verification; reconcile again')
        source_count = sum(conn.execute('SELECT count(*) FROM main.'+owner).fetchone()[0] for owner in preparation.KEYS)
        if (source_count!=count or current.copied!=count
                or conn.execute('SELECT count(*) FROM prepared_taste_rows').fetchone()[0]!=count
                or conn.execute('SELECT count(*) FROM '+table('rows')+' WHERE stage=?',(identity,)).fetchone()[0]!=count):
            raise MigrationError('Taste publication has missing or extra contributions')
        if conn.execute('SELECT 1 FROM '+table('rows')+
            " WHERE stage=? AND owner IN ('rec_weights','rec_lean') GROUP BY owner,axis_key HAVING count(*)>1 LIMIT 1",(identity,)).fetchone():
            raise MigrationError('Multiple configured taste values resolve to one identity; reconcile the source explicitly')
        token = ("CASE kind WHEN 'genre' THEN 'genre:'||genre_key WHEN 'family' THEN 'genre:'||family_key "
                 "WHEN 'unresolved_genre' THEN 'genre:'||axis_value ELSE axis_value END")
        if conn.execute('SELECT 1 FROM '+table('rows')+" WHERE stage=? AND owner<>'rec_grad_log' GROUP BY owner,"+token+
            ' HAVING count(DISTINCT axis_key)>1 LIMIT 1',(identity,)).fetchone():
            raise MigrationError('Distinct taste identities share an API token; reconcile the source or references explicitly')
        return captured,current
    finally:
        conn.rollback()


def publish(conn,*,identity,clock=time.time):
    if type(identity) is not int or identity<=0:
        raise ValueError('Taste publication requires a positive stage identity')
    install(conn)
    captured = verify(conn,identity)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if (proof(conn),stage(conn,identity))!=captured:
            raise MigrationError('Taste changed after verification; reconcile again')
        conn.execute('INSERT INTO '+table('head')+' VALUES (1,?,1,?) '
            'ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1,published_at=excluded.published_at',(identity,clock()))
        conn.commit()
    finally:
        conn.rollback()


def selected(conn):
    if taste_axes.available(conn) or conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(table('head'),)).fetchone() is None:
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


def projection(conn,owner,columns,*,token_name='axis'):
    identity = selected(conn)
    if identity is None or owner not in preparation.KEYS:
        return None
    token = ("CASE kind WHEN 'genre' THEN 'genre:'||genre_key WHEN 'family' THEN 'genre:'||family_key "
             "WHEN 'unresolved_genre' THEN 'genre:'||axis_value ELSE axis_value END AS "+token_name)
    fields = {'rec_weights':'weight,updated_at','rec_lean':'value,updated_at,last_graduated_day',
        'home_hidden_facet':'','rec_theme':'sum(score) AS score,max(updated_at) AS updated_at',
        'rec_play_grad':'max(last_graduated_day) AS last_graduated_day',
        'rec_grad_log':"json_extract(source_key,'$[0]') AS id,source,score,factor,new_weight,created_at"}[owner]
    folded = 'SELECT '+token+(','+fields if fields else '')+' FROM main.'+table('rows')
    folded += ' WHERE stage='+str(identity)+' AND owner='+repr(owner)
    if owner in ('rec_theme','rec_play_grad','home_hidden_facet'):
        folded += ' GROUP BY axis_key'
    return 'SELECT '+columns+' FROM ('+folded+') t'


def live_projection(owner,columns,*,token_name='axis'):
    """Keep canonical API folds coherent while installed sources are authoritative.

    A continuing write withdraws immutable selection. Readers then resolve the
    current physical rows, including aliases introduced after preparation. They
    must not expose separate theme contributions as separate graduation targets.
    Configured collisions retain both raw keys so neither value disappears.
    """
    legacy = 'facet' if owner=='rec_theme' else 'axis'
    fields = {'rec_weights':'weight,updated_at','rec_lean':'value,updated_at,last_graduated_day',
        'home_hidden_facet':'','rec_theme':'score,updated_at','rec_play_grad':'last_graduated_day',
        'rec_grad_log':'id,source,score,factor,new_weight,created_at'}[owner]
    source = ('SELECT '+legacy+' AS raw_token,'+taste_axes.legacy_token(legacy)+' AS canonical_token,'
        +taste_axes.legacy_kind(legacy)+' AS axis_kind'+(','+fields if fields else '')+' FROM main.'+owner)
    folded = 'SELECT canonical_token,axis_kind,'
    if owner in ('rec_theme','rec_play_grad','home_hidden_facet'):
        folded += 'min(raw_token) AS raw_token'
    else:
        folded += 'raw_token'
    if owner=='rec_theme':
        folded += ',sum(score) AS score,max(updated_at) AS updated_at'
    elif owner=='rec_play_grad':
        folded += ',max(last_graduated_day) AS last_graduated_day'
    elif fields:
        folded += ','+fields
    folded += ' FROM ('+source+')'
    if owner in ('rec_theme','rec_play_grad','home_hidden_facet'):
        folded += ' GROUP BY axis_kind,canonical_token'
    unique = 'min(axis_kind) OVER (PARTITION BY canonical_token)=max(axis_kind) OVER (PARTITION BY canonical_token)'
    if owner in ('rec_weights','rec_lean'):
        unique += ' AND count(*) OVER (PARTITION BY canonical_token)=1'
    token = 'CASE WHEN '+unique+' THEN canonical_token ELSE raw_token END'
    visible = 'SELECT '+token+' AS '+token_name+(','+fields if fields else '')+' FROM ('+folded+')'
    return 'SELECT '+columns+' FROM ('+visible+') t'


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--publish-stage',type=int)
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=16)
    args = parser.parse_args(argv)
    if not 1<=args.batches<=128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw',uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                publish(conn,identity=args.publish_stage)
                result = {'selected':selected(conn)}
            else:
                for _ in range(args.batches):
                    current = step(conn,batch_size=args.batch_size)
                    if current.complete:
                        break
                result = asdict(current)
            print(json.dumps(result))
    except (MigrationError,sqlite3.Error,ValueError) as exc:
        parser.exit(1,f'Taste publication failed: {exc}\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
