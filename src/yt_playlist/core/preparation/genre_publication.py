"""Publish checked genre selections while retaining installed label writers."""
from dataclasses import asdict, dataclass
import json
import time

from .. import genre_classifications
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import bounded, genre_classifications as preparation


PREFIX = 'published_genre_'
FIELDS = ('owner', 'source_key', 'raw_label', 'genre_key', 'disposition', 'source_digest')
MAX_BYTES = 64*1024


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
    body = dict(preparation.JOB.destinations)['prepared_genre_classifications'].split('(', 1)[1].rsplit(')', 1)[0]
    body = body.replace('PRIMARY KEY(owner,source_key)', 'PRIMARY KEY(stage,owner,source_key)')
    yield table('rows'), (f'CREATE TABLE {table("rows")} (stage INTEGER NOT NULL REFERENCES {table("stages")}(id),'
        f'source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot) AND length(CAST(source_snapshot AS BLOB))<={MAX_BYTES}),'+body+') WITHOUT ROWID')
    yield table('generations'), f'''CREATE TABLE {table('generations')} (
        revision INTEGER PRIMARY KEY REFERENCES {table('stages')}(id),proof TEXT NOT NULL CHECK(json_valid(proof)),published_at REAL NOT NULL)'''
    yield table('head'), f'''CREATE TABLE {table('head')} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {table('generations')}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1)))'''
    for owner in (table('rows'), table('generations'), table('stages'), 'prepared_genre_originals'):
        for operation in ('UPDATE', 'DELETE'):
            name = table(owner+'_'+operation.lower())
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '+
                ('WHEN OLD.complete=1 ' if owner == table('stages') else '')+
                "BEGIN SELECT RAISE(ABORT,'Retained genre publication requires future cleanup'); END")
    name = table('rows_insert')
    yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{table("rows")} '
        f'WHEN EXISTS(SELECT 1 FROM {table("stages")} WHERE id=NEW.stage AND complete=1) '
        "BEGIN SELECT RAISE(ABORT,'Completed genre publication is retained'); END")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = table('prepared_'+operation.lower())
        yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.prepared_genre_classifications BEGIN '
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
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
            raise MigrationError('Incompatible genre publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+table('epoch')+' WHERE id=1').fetchone():
        raise MigrationError('Genre publication requires its custody revision')


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return
    validate(conn)
    if not genre_classifications.available(conn):
        preparation.JOB.validate(conn)
    if conn.execute('SELECT 1 FROM '+table('head')+' h LEFT JOIN '+table('generations')+
        ' g ON g.revision=h.revision LEFT JOIN '+table('stages')+
        ' s ON s.id=g.revision WHERE s.id IS NULL OR s.complete<>1 OR g.proof<>s.proof LIMIT 1').fetchone():
        raise MigrationError('Genre selection requires its completed stage')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Genre publication requires an idle connection')
    def installed():
        preparation.JOB._version(conn)
        preparation.JOB.validate(conn)
        exists = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone()
        if exists:
            validate(conn)
        return exists
    try:
        conn.execute('BEGIN')
        exists = installed()
        conn.rollback()
        if exists:
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
    current = preparation.JOB.progress(conn)
    if (current is None or current.status != 'prepared' or current.input_revision != current.prepared_revision
            or conn.execute('SELECT 1 FROM '+preparation.JOB.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Genre publication requires reconciled preparation')
    return canonical([asdict(current), conn.execute('SELECT revision FROM '+table('epoch')+' WHERE id=1').fetchone()[0]]).decode()


def stage(conn, identity):
    row = conn.execute('SELECT * FROM '+table('stages')+' WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise MigrationError('Genre publication stage is missing')
    return Stage(*row)


def latest(conn):
    row = conn.execute('SELECT * FROM '+table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
    return Stage(*row) if row else None


def batch(conn, after_owner, after_key, size):
    query = ('SELECT p.*,o.source_snapshot FROM prepared_genre_classifications p '
        'JOIN prepared_genre_originals o ON o.owner=p.owner AND o.source_key=p.source_key AND o.digest=p.source_digest')
    parameters = ()
    if after_owner is not None:
        query += ' WHERE (p.owner,p.source_key)>(?,?)'
        parameters = (after_owner, after_key)
    return bounded.rows(conn, query+' ORDER BY p.owner,p.source_key LIMIT ?', (*parameters, size),
        byte_limit=MAX_BYTES, message='Genre publication exceeds its row byte budget')


def step(conn, *, batch_size=16):
    if type(batch_size) is not int or not 1 <= batch_size <= 16:
        raise ValueError('Genre publication requires 1 to 16 rows per batch')
    install(conn)
    try:
        conn.execute('BEGIN')
        captured, previous = proof(conn), latest(conn)
        current = previous if previous and previous.proof == captured else Stage(0, captured, None, None, 0, 0)
        if current.complete:
            return current
        rows = batch(conn, current.after_owner, current.after_key, batch_size+1)
        more, rows = len(rows)>batch_size, rows[:batch_size]
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        if proof(conn) != captured or latest(conn) != previous:
            raise MigrationError('Genre inputs changed before staging; reconcile again')
        if not current.id:
            identity = conn.execute('INSERT INTO '+table('stages')+
                '(proof,copied,complete) VALUES (?,0,0)', (captured,)).lastrowid
            current = stage(conn, identity)
        for row in rows:
            conn.execute('INSERT INTO '+table('rows')+'(stage,'+','.join(row)+') VALUES ('+
                ','.join('?' for _ in range(len(row)+1))+')', (current.id, *row.values()))
        conn.execute('UPDATE '+table('stages')+' SET after_owner=?,after_key=?,copied=?,complete=? WHERE id=?',
            (rows[-1]['owner'] if rows else current.after_owner, rows[-1]['source_key'] if rows else current.after_key,
             current.copied+len(rows), int(not more), current.id))
        result = stage(conn, current.id)
        conn.commit()
        return result
    finally:
        conn.rollback()


def verify(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Genre verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        validate_installed(conn)
        captured, current = proof(conn), stage(conn, identity)
        if not current.complete or current.proof != captured:
            raise MigrationError('Genre publication stage is incomplete or changed')
    finally:
        conn.rollback()
    after_owner, after_key, count = None, None, 0
    while True:
        try:
            conn.execute('BEGIN')
            if proof(conn) != captured:
                raise MigrationError('Genre inputs changed during verification; reconcile again')
            rows = batch(conn, after_owner, after_key, 16)
            for row in rows:
                source = preparation.JOB._read(conn, row['owner'], row['source_key'])
                if source is None:
                    raise MigrationError('Genre publication has no installed source')
                expected, snapshot = preparation.describe(conn, row['owner'], row['source_key'], source)
                actual = conn.execute('SELECT * FROM '+table('rows')+' WHERE stage=? AND owner=? AND source_key=?',
                    (identity, row['owner'], row['source_key'])).fetchone()
                if (tuple(row[field] for field in FIELDS) != expected or row['source_snapshot'] != snapshot
                        or actual is None or {key:actual[key] for key in row} != row):
                    raise MigrationError('Genre publication differs from its original or prepared selection')
                if row['genre_key'] is not None and not conn.execute('SELECT 1 FROM main.genre_catalog WHERE key=?',
                        (row['genre_key'],)).fetchone():
                    raise MigrationError('Genre publication references a missing genre')
            if not rows:
                break
            after_owner, after_key = rows[-1]['owner'], rows[-1]['source_key']
            count += len(rows)
        finally:
            conn.rollback()
    source_count = sum(conn.execute('SELECT count(*) FROM main.'+owner).fetchone()[0] for owner in preparation.JOB.keys)
    if (source_count != count or current.copied != count
            or conn.execute('SELECT count(*) FROM '+table('rows')+' WHERE stage=?', (identity,)).fetchone()[0] != count
            or conn.execute('SELECT count(*) FROM prepared_genre_classifications').fetchone()[0] != count):
        raise MigrationError('Genre publication has missing or extra selections')
    try:
        conn.execute('BEGIN')
        if (proof(conn), stage(conn, identity)) != (captured, current):
            raise MigrationError('Genre inputs changed during verification; reconcile again')
    finally:
        conn.rollback()
    return captured, current


def publish(conn, *, identity, clock=time.time):
    if type(identity) is not int or identity<=0:
        raise ValueError('Genre publication requires a positive stage identity')
    install(conn)
    captured = verify(conn, identity)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if (proof(conn), stage(conn, identity)) != captured:
            raise MigrationError('Genre inputs changed after verification; reconcile again')
        conn.execute('INSERT INTO '+table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING', (identity, captured[0], clock()))
        conn.execute('INSERT INTO '+table('head')+' VALUES (1,?,1) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1', (identity,))
        conn.commit()
    finally:
        conn.rollback()


def selected(conn):
    if genre_classifications.available(conn) or not conn.execute(
            'SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


def expression(conn, alias, owner):
    identity = selected(conn)
    if identity is None:
        return alias+'.genre'
    column = genre_classifications.OWNERS[owner]
    return ('(SELECT CASE WHEN p.disposition=\'resolved\' THEN g.name ELSE p.raw_label END FROM main.'+
        table('rows')+' p LEFT JOIN main.genre_catalog g ON g.key=p.genre_key WHERE p.stage='+str(identity)+
        ' AND p.owner='+repr(owner)+' AND p.source_key=json_array('+alias+'.'+column+'))')


def install_reads(conn):
    """Bind a finite read family to this private connection's pinned selection."""
    if selected(conn) is None:
        return
    installed = list(getattr(conn, '_entity_read_views', ()))
    from . import discovery_publication
    discovery = discovery_publication.OWNERS['legacy']
    names = ('catalog_tracks', 'tracks', 'playable_library_tracks',
             'discovered_tracks', 'catalog_enrichment_tracks', 'discovered_albums',
             discovery.table('discovered_tracks'), discovery.table('discovered_albums'))
    for name in names:
        row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='view' AND name=?", (name,)).fetchone()
        if row is None:
            continue
        sql = row[0]
        if name == 'catalog_tracks':
            sql = sql.replace('r.genre END AS genre', expression(conn, 'r', 'recordings')+' END AS genre')
        elif name == 'discovered_albums':
            sql = sql.replace('a.genre,', expression(conn, 'a', 'albums')+' AS genre,')
        elif name == discovery.table('discovered_tracks'):
            sql = sql.replace('main.catalog_tracks', 'temp.catalog_tracks')
        elif name == discovery.table('discovered_albums'):
            sql = sql.replace('a.genre genre,', expression(conn, 'a', 'albums')+' AS genre,')
        conn.execute('DROP VIEW IF EXISTS temp.'+name)
        conn.execute(sql.replace('CREATE VIEW ', 'CREATE TEMP VIEW ', 1))
        if name not in installed:
            installed.append(name)
    conn._entity_read_views = installed


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
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                publish(conn, identity=args.publish_stage)
                print(json.dumps({'selected': selected(conn)}))
            else:
                for _ in range(args.batches):
                    current = step(conn, batch_size=args.batch_size)
                    if current.complete:
                        break
                print(json.dumps(asdict(current)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Genre publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
