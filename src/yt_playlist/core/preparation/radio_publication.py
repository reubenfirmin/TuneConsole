"""Select an exact retained Radio checkpoint after independent verification.

Verification, file reads and queue reconciliation precede the writer reservation.
Source/reference writes withdraw selection in their own transaction. Compatible
writers keep the installed wire document; old published generations stay retained.
"""
import json
import sqlite3
import time
from dataclasses import dataclass

from .. import collection_recovery, entity_names
from ..migrations.runner import MigrationError
from . import radio_session as preparation
from .selection import invalidators


GENERATIONS = 'published_radio_generations'
HEAD = 'published_radio_head'
NAME = 'radio-session'
EPOCH = 'published_radio_custody_epoch'


@dataclass(frozen=True)
class Proof:
    progress: object
    header: tuple
    epoch: int


def definitions():
    yield GENERATIONS, f'''CREATE TABLE {GENERATIONS} (
        revision INTEGER PRIMARY KEY REFERENCES {preparation.HEAD}(generation),published_at REAL NOT NULL)'''
    yield HEAD, f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name='radio-session'),revision INTEGER NOT NULL REFERENCES {GENERATIONS}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
    yield EPOCH, f'''CREATE TABLE {EPOCH} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'''
    for operation in ('UPDATE','DELETE'):
        name = GENERATIONS+'_'+operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{GENERATIONS} '
            "BEGIN SELECT RAISE(ABORT,'Published Radio generations require future cleanup'); END")
    for table in (preparation.HEAD,preparation.QUEUE):
        for operation in ('INSERT','UPDATE','DELETE'):
            sides = ('OLD','NEW') if operation == 'UPDATE' else ('NEW',) if operation == 'INSERT' else ('OLD',)
            condition = ' OR '.join('revision='+side+'.generation' for side in sides)
            name = table+'_published_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table} '
                f'WHEN EXISTS(SELECT 1 FROM {GENERATIONS} WHERE {condition}) '
                "BEGIN SELECT RAISE(ABORT,'Published Radio custody is retained'); END")
            name = table+'_custody_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{table} '
                f'BEGIN UPDATE {EPOCH} SET revision=revision+1 WHERE id=1; END')
    yield from invalidators(HEAD,{NAME:preparation.DEFINITION})


def validate(conn):
    from ..migrations.current import _tokens
    from ..entity_names import predecessor_definition as transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone()
    for name,sql in definitions():
        if renamed:
            sql = transform(sql)
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible Radio publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+EPOCH+' WHERE id=1').fetchone():
        raise MigrationError('Radio publication requires its custody revision')


def install(conn):
    """Install finite empty publication owners and the preparation mutation fence."""
    if conn.in_transaction:
        raise MigrationError('Radio publication requires an idle connection')
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
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='radio_sessions'").fetchone():
        return None
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(HEAD,)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+HEAD+' WHERE name=? AND active=1',(NAME,)).fetchone()
    return row[0] if row else None


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(HEAD,)).fetchone():
        return
    validate(conn)
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='radio_sessions'").fetchone():
        return
    preparation.DEFINITION.validate(conn)
    if conn.execute(f'SELECT 1 FROM {HEAD} h LEFT JOIN {GENERATIONS} g ON g.revision=h.revision '
        f'LEFT JOIN {preparation.HEAD} s ON s.generation=g.revision WHERE g.revision IS NULL OR s.generation IS NULL LIMIT 1').fetchone():
        raise MigrationError('Radio selection requires its retained committed generation')


def value(conn, revision, default=None):
    row = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?',(revision,)).fetchone()
    if row is None:
        raise MigrationError('Published Radio checkpoint is missing')
    return preparation.read(row) if row['source_present'] else default


def verify(conn, revision):
    """Perform bulk proof work outside the publication writer reservation."""
    if conn.in_transaction:
        raise MigrationError('Radio verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        validate(conn)
        current = preparation.JOB.progress(conn)
        if (current is None or current.status != 'prepared' or current.input_revision != revision
                or current.prepared_revision != revision):
            raise MigrationError('Radio publication requires its reconciled preparation revision')
        header = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?',(revision,)).fetchone()
        if header is None:
            raise MigrationError('Prepared Radio checkpoint is missing')
        proof = Proof(current,tuple(header),conn.execute('SELECT revision FROM '+EPOCH+' WHERE id=1').fetchone()[0])
    finally:
        conn.rollback()
    # Files and decoding never run under a SQLite transaction. The subsequent
    # read snapshot checks this same captured revision before accepting them.
    raw = preparation.read(header)
    state,queue,_ = preparation._decode(raw)
    try:
        conn.execute('BEGIN')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        if not unchanged(conn,proof,revision):
            raise MigrationError('Radio input changed during verification; reconcile before publication')
        source = conn.execute("SELECT COALESCE(substr(CAST(value AS BLOB),1,?),X''),value IS NULL "
            'FROM main.settings WHERE key=?',(collection_recovery.MAX_BYTES+1,preparation.KEY)).fetchone()
        wire = bytes(source[0]).decode('utf-8') if source and not source[1] else None
        if bool(source) != bool(header['source_present']) or wire != raw:
            raise MigrationError('Prepared Radio original differs from its installed checkpoint')
        expected = (int(state is not None),state.get('episode_id') if state else None,
            'missing_episode_id' if state and state.get('episode_id') is None else None,
            state['identity'] if state else None,state['phase'] if state else None,
            state.get('playlist') if state else None,len(queue))
        if tuple(header[column] for column in ('active','episode_id','episode_resolution','account_id','phase','playlist_provider_id','queue_size')) != expected:
            raise MigrationError('Prepared Radio fields differ from their verified checkpoint')
        if state and not conn.execute('SELECT 1 FROM '+entity_names.owner(conn,'accounts').table+' WHERE id=?',(state['identity'],)).fetchone():
            raise MigrationError('Radio publication requires its recorded account')
        rows = conn.execute('SELECT position,section,section_position,state,video_id,recording_id,entry_id,unresolved_reason FROM '+
                            preparation.QUEUE+' WHERE generation=? ORDER BY position',(revision,))
        for position,(section,index,status,track) in enumerate(queue):
            actual = rows.fetchone()
            video = track['video_id']
            if actual is None or tuple(actual) != (position,section,index,status,video,*preparation.subject(conn,video)):
                raise MigrationError('Prepared Radio queue differs from its verified checkpoint')
        if rows.fetchone() is not None or conn.execute('SELECT 1 FROM '+preparation.DEFINITION.changes+' LIMIT 1').fetchone():
            raise MigrationError('Radio publication requires its complete reconciled queue')
        return proof
    finally:
        conn.rollback()


def unchanged(conn, proof, revision):
    return (preparation.JOB.progress(conn) == proof.progress
        and conn.execute('SELECT revision FROM '+EPOCH+' WHERE id=1').fetchone()[0] == proof.epoch
        and tuple(conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?',(revision,)).fetchone() or ()) == proof.header)


def publish(conn, *, revision, clock=time.time):
    if type(revision) is not int or revision < 0:
        raise ValueError('Radio publication requires a nonnegative input revision')
    install(conn)
    proof = verify(conn,revision)
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        validate(conn)
        if not unchanged(conn,proof,revision):
            raise MigrationError('Radio input changed after verification; reconcile before publication')
        if not conn.execute('SELECT 1 FROM '+GENERATIONS+' WHERE revision=?',(revision,)).fetchone():
            conn.execute('INSERT INTO '+GENERATIONS+' VALUES (?,?)',(revision,clock()))
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
        parser.exit(1,f'Radio publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
