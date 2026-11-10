"""Select verified Radio origins without guessing scopes or retiring settings."""
from dataclasses import dataclass
import json
import sqlite3
import time

from .. import radio_provenance
from ..schema_contract import MigrationError
from . import radio_origins as preparation
from .selection import invalidators


NAME = 'radio-origins'
GENERATIONS = 'published_radio_origin_generations'
HEAD = 'published_radio_origin_head'
LINKS = 'published_radio_origin_sources'
EPOCH = 'published_radio_origin_custody_epoch'


@dataclass(frozen=True)
class Proof:
    progress: object
    headers: tuple
    epoch: int


def definitions():
    yield GENERATIONS,f'''CREATE TABLE {GENERATIONS} (
        revision INTEGER PRIMARY KEY CHECK(revision>=0),published_at REAL NOT NULL)'''
    yield HEAD,f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name={NAME!r}),revision INTEGER NOT NULL REFERENCES {GENERATIONS}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
    yield LINKS,f'''CREATE TABLE {LINKS} (
        revision INTEGER NOT NULL REFERENCES {GENERATIONS}(revision),
        source TEXT NOT NULL CHECK(source IN {preparation.KEYS!r}),generation INTEGER NOT NULL,
        PRIMARY KEY(revision,source),FOREIGN KEY(source,generation)
            REFERENCES {preparation.HEAD}(source,generation)) WITHOUT ROWID'''
    yield EPOCH,f'''CREATE TABLE {EPOCH} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'''
    for owner in (GENERATIONS,LINKS):
        for operation in ('UPDATE','DELETE'):
            name = owner+'_'+operation.lower()
            yield name,(f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                "BEGIN SELECT RAISE(ABORT,'Published Radio origins require future cleanup'); END")
    for owner in (preparation.HEAD,preparation.PARTS,preparation.ORIGINS,preparation.CURRENT):
        for operation in ('INSERT','UPDATE','DELETE'):
            if owner != preparation.CURRENT:
                sides = ('OLD','NEW') if operation == 'UPDATE' else ('NEW',) if operation == 'INSERT' else ('OLD',)
                condition = ' OR '.join(f'(source={side}.source AND generation={side}.generation)' for side in sides)
                name = owner+'_published_'+operation.lower()
                yield name,(f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                    f'WHEN EXISTS(SELECT 1 FROM {LINKS} WHERE {condition}) '
                    "BEGIN SELECT RAISE(ABORT,'Published Radio origin custody is retained'); END")
            name = owner+'_custody_'+operation.lower()
            yield name,(f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                f'BEGIN UPDATE {EPOCH} SET revision=revision+1 WHERE id=1; END')
    # The lookup occurs per origin member during ordinary preparation writes.
    yield 'ix_published_radio_origin_custody',f'CREATE INDEX ix_published_radio_origin_custody ON {LINKS}(source,generation)'
    yield from invalidators(HEAD,{NAME:preparation.DEFINITION})


def validate(conn):
    from ..schema_contract import tokens as _tokens
    for name,sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible Radio origin publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+EPOCH+' WHERE id=1').fetchone():
        raise MigrationError('Radio origin publication requires its custody revision')


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(HEAD,)).fetchone():
        return
    validate(conn)
    if not radio_provenance.available(conn):
        preparation.DEFINITION.validate(conn)
    if conn.execute('SELECT 1 FROM '+HEAD+' h LEFT JOIN '+GENERATIONS+' g ON g.revision=h.revision '
        'WHERE g.revision IS NULL LIMIT 1').fetchone():
        raise MigrationError('Radio origin selection requires its committed generation')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Radio origin publication requires an idle connection')
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(HEAD,)).fetchone():
            validate(conn)
        else:
            for _,sql in definitions():
                conn.execute(sql)
            conn.execute('INSERT INTO '+EPOCH+' VALUES (1,0)')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def selected(conn):
    if radio_provenance.available(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(HEAD,)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+HEAD+' WHERE name=? AND active=1',(NAME,)).fetchone()
    return row[0] if row else None


def value(conn,source,revision,default=None):
    row = conn.execute('SELECT g.* FROM '+LINKS+' l JOIN '+preparation.HEAD+
        ' g USING(source,generation) WHERE l.revision=? AND l.source=?',(revision,source)).fetchone()
    return preparation.read_original(conn,row) if row else default


def ids(conn,revision):
    order = 'CASE o.source '+' '.join('WHEN '+repr(source)+' THEN '+str(index) for index,source in enumerate(preparation.KEYS))+' END'
    rows = conn.execute('SELECT o.provider_playlist_id FROM '+LINKS+' l JOIN '+preparation.ORIGINS+
        ' o USING(source,generation) WHERE l.revision=? ORDER BY '+order+',o.position',(revision,))
    return list(dict.fromkeys(row[0] for row in rows))


def capture(conn):
    return conn.execute('SELECT g.* FROM '+preparation.CURRENT+' h JOIN '+preparation.HEAD+
        ' g USING(source,generation) ORDER BY source LIMIT 5').fetchall()


def proof(conn,revision):
    current = preparation.JOB.progress(conn)
    if (current is None or current.status != 'prepared' or current.input_revision != revision
            or current.prepared_revision != revision
            or conn.execute('SELECT 1 FROM '+preparation.DEFINITION.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Radio origin publication requires its reconciled preparation revision')
    headers = capture(conn)
    if len(headers) > len(preparation.KEYS):
        raise MigrationError('Radio origin publication exceeds its finite source set')
    return Proof(current,tuple(tuple(row) for row in headers),
        conn.execute('SELECT revision FROM '+EPOCH+' WHERE id=1').fetchone()[0])


def verify(conn,revision):
    if conn.in_transaction:
        raise MigrationError('Radio origin verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        validate(conn)
        captured = proof(conn,revision)
        headers = capture(conn)
        sources = {row[0]:bytes(row[1]).decode('utf-8') if row[1] is not None else None for row in conn.execute(
            "SELECT key,CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,?),X'') END "
            'FROM main.settings WHERE key IN (?,?,?,?)',(preparation.MAX_BYTES+1,*preparation.KEYS))}
    finally:
        conn.rollback()
    if {row['source'] for row in headers} != sources.keys():
        raise MigrationError('Prepared Radio origins differ from their installed source selection')
    decoded = {}
    for header in headers:
        source = header['source']
        if preparation.read_original(conn,header) != sources[source]:
            raise MigrationError('Prepared Radio origin differs from its installed original')
        text,values,kind = preparation.decode(source,sources[source])
        expected = (len(text.encode()),len(range(0,len(text),preparation.PART_CHARACTERS)),len(values),kind)
        if tuple(header[field] for field in ('raw_size','part_count','item_count','source_kind')) != expected:
            raise MigrationError('Prepared Radio origin fields differ from their verified original')
        decoded[source] = values
    try:
        conn.execute('BEGIN')
        if proof(conn,revision) != captured:
            raise MigrationError('Radio origins changed during verification; reconcile before publication')
        references = {}
        for header in headers:
            rows = conn.execute('SELECT position,provider_playlist_id,playlist_scope_id,unresolved_reason FROM '+preparation.ORIGINS+
                ' WHERE source=? AND generation=? ORDER BY position',(header['source'],header['generation']))
            for position,provider in enumerate(decoded[header['source']]):
                # All rows are checked against the same pinned scope snapshot.
                if provider not in references:
                    references[provider] = preparation.reference(conn,provider)
                actual = rows.fetchone()
                if actual is None or tuple(actual) != (position,provider,*references[provider]):
                    raise MigrationError('Prepared Radio origin members differ from their verified original')
            if rows.fetchone() is not None:
                raise MigrationError('Prepared Radio origin contains extra members')
        return captured
    finally:
        conn.rollback()


def publish(conn,*,revision,clock=time.time):
    if type(revision) is not int or revision < 0:
        raise ValueError('Radio origin publication requires a nonnegative revision')
    install(conn)
    captured = verify(conn,revision)
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        validate(conn)
        if proof(conn,revision) != captured:
            raise MigrationError('Radio origins changed after verification; reconcile before publication')
        if not conn.execute('SELECT 1 FROM '+GENERATIONS+' WHERE revision=?',(revision,)).fetchone():
            conn.execute('INSERT INTO '+GENERATIONS+' VALUES (?,?)',(revision,clock()))
            conn.executemany('INSERT INTO '+LINKS+' VALUES (?,?,?)',
                [(revision,row['source'],row['generation']) for row in capture(conn)])
        conn.execute('INSERT INTO '+HEAD+' VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1',(NAME,revision))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--revision',type=int,required=True)
    args = parser.parse_args(argv)
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw',uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            publish(conn,revision=args.revision)
            print(json.dumps({'revision':selected(conn)}))
    except (MigrationError,sqlite3.Error,ValueError,OSError) as exc:
        parser.exit(1,f'Radio origin publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
