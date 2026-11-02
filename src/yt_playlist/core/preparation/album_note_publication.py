"""Select retained album-note files without retiring installed proposal writers."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time

from .. import album_note_requests, recommendation_outputs as files
from ..schema_contract import MigrationError
from ..policy_documents import _publish_encoded
from . import album_notes as preparation
from .selection import invalidators


NAME = preparation.SURFACE
GENERATIONS = 'published_album_note_generations'
HEAD = 'published_album_note_head'
EPOCH = 'published_album_note_epoch'


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='album_note_outputs'").fetchone() is not None


def definitions():
    yield GENERATIONS, f'''CREATE TABLE {GENERATIONS} (
        revision INTEGER PRIMARY KEY REFERENCES {preparation.HEAD}(generation),published_at REAL NOT NULL)'''
    yield HEAD, f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name={NAME!r}),revision INTEGER NOT NULL REFERENCES {GENERATIONS}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
    yield EPOCH, f'CREATE TABLE {EPOCH} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
    for operation in ('UPDATE','DELETE'):
        name = GENERATIONS+'_'+operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{GENERATIONS} '
            "BEGIN SELECT RAISE(ABORT,'Published album notes require future cleanup'); END")
    for owner in (preparation.HEAD, preparation.FILES):
        for operation in ('INSERT','UPDATE','DELETE'):
            sides = ('OLD','NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
            condition = ' OR '.join('revision='+side+'.generation' for side in sides)
            name = owner+'_published_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                f'WHEN EXISTS(SELECT 1 FROM {GENERATIONS} WHERE {condition}) '
                "BEGIN SELECT RAISE(ABORT,'Published album-note custody is retained'); END")
            name = owner+'_publication_epoch_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                f'BEGIN UPDATE {EPOCH} SET revision=revision+1 WHERE id=1; END')
    yield from invalidators(HEAD, {NAME:preparation.DEFINITION})
    name = HEAD+'_input_changed'
    yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{preparation.DEFINITION.state} '
        f'WHEN NEW.input_revision<>OLD.input_revision BEGIN UPDATE {HEAD} SET active=0 WHERE active=1; END')


def validate(conn):
    from ..schema_contract import tokens as _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible album-note publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+EPOCH+' WHERE id=1').fetchone():
        raise MigrationError('Album-note publication requires its custody revision')


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    validate(conn)
    if not native(conn):
        preparation.DEFINITION.validate(conn)
    if conn.execute('SELECT 1 FROM '+HEAD+' h LEFT JOIN '+GENERATIONS+' g ON g.revision=h.revision '
            'LEFT JOIN '+preparation.HEAD+' p ON p.generation=g.revision WHERE g.revision IS NULL OR p.generation IS NULL LIMIT 1').fetchone():
        raise MigrationError('Album-note selection requires its retained generation')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Album-note publication requires an idle connection')
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
        else:
            for _, sql in definitions():
                conn.execute(sql)
            conn.execute('INSERT INTO '+EPOCH+' VALUES (1,0)')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@dataclass(frozen=True)
class Proof:
    progress: object
    header: tuple
    epoch: int


def proof(conn, revision):
    progress = preparation.JOB.progress(conn)
    if (progress is None or progress.status != 'prepared' or progress.input_revision != revision or
            progress.prepared_revision != revision or conn.execute('SELECT 1 FROM '+preparation.DEFINITION.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Album notes require their reconciled preparation revision')
    header = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?', (revision,)).fetchone()
    if header is None:
        raise MigrationError('Album notes require their retained source generation')
    return Proof(progress, tuple(header), conn.execute('SELECT revision FROM '+EPOCH+' WHERE id=1').fetchone()[0])


def source(conn):
    return conn.execute('SELECT substr(CAST(payload AS BLOB),1,?),built_at FROM main.rec_proposals WHERE surface=?',
        (preparation.MAX_SOURCE_BYTES+1, NAME)).fetchone()


def verify(conn, revision):
    if conn.in_transaction:
        raise MigrationError('Album-note verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        validate(conn)
        captured = proof(conn, revision)
        header = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?', (revision,)).fetchone()
        rows = conn.execute('SELECT * FROM '+preparation.FILES+' WHERE generation=? ORDER BY position', (revision,)).fetchall()
        original = source(conn)
    finally:
        conn.rollback()
    encoded, notes = preparation._decode(original)
    if (header['source_present'] != int(original is not None) or header['notes_count'] != len(notes) or
            header['built_at'] != (original[1] if original else None) or
            preparation.read_original(header) != (encoded.decode() if encoded is not None else None)):
        raise MigrationError('Album-note original differs from its installed source')
    if len(rows) != len(notes):
        raise MigrationError('Album notes contain missing or extra outputs')
    for position, ((browse, note), row) in enumerate(zip(notes, rows, strict=True)):
        expected = files.encode(NAME, browse, note)
        reference, reason = album_note_requests._reference(conn, browse)
        if ((row['position'],row['browse_id'],row['album_provider_id'],row['unresolved_reason']) != (position,browse,reference,reason) or
                row['digest'] != hashlib.sha256(expected).hexdigest() or row['byte_size'] != len(expected) or
                files._read(row['filename'],row['digest'],row['byte_size'],surface=NAME,chunk_key=browse) != expected):
            raise MigrationError('Album-note output differs from its original or references')
    try:
        conn.execute('BEGIN')
        if proof(conn, revision) != captured:
            raise MigrationError('Album notes changed during verification; reconcile again')
    finally:
        conn.rollback()
    return captured


def publish(conn, *, revision, clock=time.time):
    if type(revision) is not int or revision < 0:
        raise ValueError('Album-note publication requires a nonnegative revision')
    install(conn)
    captured = verify(conn, revision)
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.DEFINITION._version(conn)
        preparation.DEFINITION.validate(conn)
        validate(conn)
        if proof(conn, revision) != captured:
            raise MigrationError('Album notes changed after verification; reconcile again')
        conn.execute('INSERT INTO '+GENERATIONS+' VALUES (?,?) ON CONFLICT DO NOTHING', (revision, clock()))
        conn.execute('INSERT INTO '+HEAD+' VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1', (NAME, revision))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def selected(conn):
    if native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+HEAD+' WHERE active=1').fetchone()
    return row[0] if row else None


def read(conn):
    """Capture one head and immutable manifests in the caller's snapshot."""
    owned = not conn.in_transaction
    try:
        if owned:
            conn.execute('BEGIN')
        revision = selected(conn)
        if revision is None:
            return False, None
        rows = conn.execute('SELECT * FROM '+preparation.FILES+' WHERE generation=? ORDER BY position', (revision,)).fetchall()
    finally:
        if owned:
            conn.rollback()
    value = {row['browse_id']:files.read(row['filename'],row['digest'],row['byte_size'],surface=NAME,chunk_key=row['browse_id']) for row in rows}
    return True, json.dumps(value) if value else None


def repair(conn):
    """Restore exact missing files for an unchanged captured source, without SQL edits."""
    if conn.in_transaction:
        raise MigrationError('Album-note file recovery requires an idle connection')
    progress = preparation.JOB.progress(conn)
    if progress is None or progress.prepared_revision != progress.input_revision:
        raise MigrationError('Album-note source changed; reconcile before recovery')
    header = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?', (progress.input_revision,)).fetchone()
    original = source(conn)
    encoded, notes = preparation._decode(original)
    if (header is None or header['source_present'] != int(original is not None) or header['notes_count'] != len(notes) or
            header['built_at'] != (original[1] if original else None) or
            header['digest'] != (hashlib.sha256(encoded).hexdigest() if encoded is not None else None)):
        raise MigrationError('Album-note source differs from captured custody; reconcile before recovery')
    root = preparation.directory(conn)
    if encoded is not None and _publish_encoded(root/'originals', encoded, header['digest']) != header['filename']:
        raise MigrationError('Recovered album-note original differs from its captured path')
    rows = conn.execute('SELECT * FROM '+preparation.FILES+' WHERE generation=? ORDER BY position', (header['generation'],)).fetchall()
    if len(rows) != len(notes):
        raise MigrationError('Album-note SQL custody requires repair before file recovery')
    for position, ((browse, note), row) in enumerate(zip(notes, rows, strict=True)):
        restored = files.publish(root/'outputs', files.encode(NAME, browse, note))
        if (row['position'],row['browse_id'],row['filename'],row['digest'],row['byte_size']) != (position,browse,*restored):
            raise MigrationError('Recovered album note differs from its captured manifest')


def main(argv=None):
    import argparse
    from contextlib import closing
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--revision', type=int)
    action.add_argument('--repair-files', action='store_true')
    args = parser.parse_args(argv)
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.repair_files:
                repair(conn)
                print(json.dumps({'repaired':True}))
            else:
                publish(conn, revision=args.revision)
                print(json.dumps({'selected':selected(conn)}))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Album-note publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
