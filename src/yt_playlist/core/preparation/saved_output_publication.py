"""Select verified saved outputs independently, retaining compatible source writes."""
from dataclasses import dataclass
import json
import sqlite3
import time

from .. import recommendation_setting_outputs as outputs
from ..schema_contract import MigrationError
from . import saved_outputs as preparation
from .selection import invalidators


@dataclass(frozen=True)
class Proof:
    progress: object
    header: tuple | None
    epoch: int


def job(source):
    return preparation.JOBS[next(name for name,key in preparation.SOURCES.items() if key == source)].definition


def tables(source):
    prefix = preparation.PREFIXES[source].replace('prepared_','published_',1)
    return prefix+'_generations',prefix+'_head',prefix+'_custody_epoch'


def definitions(source):
    generations,head,epoch = tables(source)
    prepared,current = preparation.tables(source)
    yield generations,f'''CREATE TABLE {generations} (
        revision INTEGER PRIMARY KEY CHECK(revision>=0),source TEXT NOT NULL CHECK(source={source!r}),
        generation INTEGER,published_at REAL NOT NULL,
        FOREIGN KEY(source,generation) REFERENCES {prepared}(source,preparation_revision))'''
    yield head,f'''CREATE TABLE {head} (
        name TEXT PRIMARY KEY CHECK(name={source!r}),revision INTEGER NOT NULL REFERENCES {generations}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
    yield epoch,f'''CREATE TABLE {epoch} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'''
    yield generations+'_custody',f'CREATE INDEX {generations}_custody ON {generations}(generation)'
    for operation in ('UPDATE','DELETE'):
        name = generations+'_'+operation.lower()
        yield name,(f'CREATE TRIGGER {name} BEFORE {operation} ON main.{generations} '
            "BEGIN SELECT RAISE(ABORT,'Published saved outputs require future cleanup'); END")
    owners = (prepared,current,preparation.SUBJECTS,preparation.POLICIES) if source == 'radio_seed_offer' else (prepared,current)
    for owner in owners:
        for operation in ('INSERT','UPDATE','DELETE'):
            if owner != current:
                sides = ('OLD','NEW') if operation == 'UPDATE' else ('NEW',) if operation == 'INSERT' else ('OLD',)
                condition = ' OR '.join('generation='+side+'.preparation_revision' for side in sides)
                name = owner+'_published_'+operation.lower()
                yield name,(f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '
                    f'WHEN EXISTS(SELECT 1 FROM {generations} WHERE {condition}) '
                    "BEGIN SELECT RAISE(ABORT,'Published saved output custody is retained'); END")
            name = owner+'_custody_'+operation.lower()
            yield name,(f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                f'BEGIN UPDATE {epoch} SET revision=revision+1 WHERE id=1; END')
    yield from invalidators(head,{source:job(source)})


def validate(conn,source):
    from ..schema_contract import tokens as _tokens
    for name,sql in definitions(source):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible saved output publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+tables(source)[2]+' WHERE id=1').fetchone():
        raise MigrationError('Saved output publication requires its custody revision')


def validate_installed(conn):
    for source in preparation.PREFIXES:
        generations,head,_ = tables(source)
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(head,)).fetchone():
            continue
        validate(conn,source)
        if not outputs.available(conn):
            job(source).validate(conn)
        if conn.execute('SELECT 1 FROM '+head+' h LEFT JOIN '+generations+' g ON g.revision=h.revision '
            'WHERE g.revision IS NULL LIMIT 1').fetchone():
            raise MigrationError('Saved output selection requires its committed generation')


def install(conn,source):
    if conn.in_transaction:
        raise MigrationError('Saved output publication requires an idle connection')
    try:
        conn.execute('BEGIN IMMEDIATE')
        definition = job(source)
        definition._version(conn)
        definition.validate(conn)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(tables(source)[1],)).fetchone():
            validate(conn,source)
        else:
            for _,sql in definitions(source):
                conn.execute(sql)
            conn.execute('INSERT INTO '+tables(source)[2]+' VALUES (1,0)')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def selected(conn,source):
    if outputs.available(conn):
        return None
    _,head,_ = tables(source)
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(head,)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+head+' WHERE name=? AND active=1',(source,)).fetchone()
    return row[0] if row else None


def value(conn,source,revision,default=None):
    row = conn.execute('SELECT generation FROM '+tables(source)[0]+' WHERE revision=?',(revision,)).fetchone()
    if row is None:
        raise MigrationError('Published saved output generation is missing')
    if row[0] is None:
        return default
    captured = conn.execute('SELECT * FROM '+preparation.tables(source)[0]+
        ' WHERE source=? AND preparation_revision=?',(source,row[0])).fetchone()
    if captured is None:
        raise MigrationError('Published saved output file reference is missing')
    return preparation.read_original(captured)


def capture(conn,source):
    prepared,current = preparation.tables(source)
    return conn.execute('SELECT g.* FROM '+current+' h JOIN '+prepared+
        ' g USING(source,preparation_revision) WHERE h.source=?',(source,)).fetchone()


def proof(conn,source,revision):
    definition = job(source)
    current = definition.progress(conn)
    if (current is None or current.status != 'prepared' or current.input_revision != revision
            or current.prepared_revision != revision
            or conn.execute('SELECT 1 FROM '+definition.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Saved output publication requires its reconciled preparation revision')
    header = capture(conn,source)
    return Proof(current,tuple(header) if header is not None else None,
        conn.execute('SELECT revision FROM '+tables(source)[2]+' WHERE id=1').fetchone()[0])


def verify(conn,source,revision):
    if conn.in_transaction:
        raise MigrationError('Saved output verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        definition = job(source)
        definition._version(conn)
        definition.validate(conn)
        validate(conn,source)
        captured = proof(conn,source,revision)
        header = capture(conn,source)
        row = conn.execute("SELECT CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,?),X'') END "
            'FROM main.settings WHERE key=?',(outputs.MAX_RAW_BYTES+1,source)).fetchone()
        present = row is not None
        raw = bytes(row[0]).decode('utf-8') if row and row[0] is not None else None
    finally:
        conn.rollback()
    if present != (header is not None):
        raise MigrationError('Prepared saved output differs from its installed source presence')
    # Hashing and decoding happen without a SQLite transaction.
    if header is not None and preparation.read_original(header) != raw:
        raise MigrationError('Prepared saved output differs from its installed original')
    kind,size,fields,units,_ = preparation.describe(source,raw)
    try:
        conn.execute('BEGIN')
        if proof(conn,source,revision) != captured:
            raise MigrationError('Saved output changed during verification; reconcile before publication')
        if header is None:
            return captured
        if (header['source_kind'],header['raw_size']) != (kind,size):
            raise MigrationError('Prepared saved output kind or size differs from its verified original')
        if source == 'rec_autotune_result':
            if header['observed_at'] != fields['observed_at']:
                raise MigrationError('Prepared historical report differs from its captured observation time')
            return captured
        run = fields['comparison_run_id']
        known = run is not None and conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?',(run,)).fetchone()
        expected = (fields['menu_id'],run,run if known else None,None if run is None or known else 'missing_run',
            fields['subject_count'],fields['policy_count'])
        if tuple(header[field] for field in ('menu_id','comparison_run_id','run_ref','run_resolution','subject_count','policy_count')) != expected:
            raise MigrationError('Prepared saved seed fields differ from their verified original')
        rows = conn.execute('SELECT position,video_id,entry_id,resolution_reason FROM '+preparation.SUBJECTS+
            ' WHERE source=? AND preparation_revision=? ORDER BY position',(source,header['preparation_revision']))
        for _,unit in units[:fields['subject_count']]:
            position,video = unit
            entry = conn.execute('SELECT id FROM catalog_entries WHERE video_id=? ORDER BY id LIMIT 1',(video,)).fetchone()
            actual = rows.fetchone()
            if actual is None or tuple(actual) != (position,video,entry[0] if entry else None,None if entry else 'missing_entry'):
                raise MigrationError('Prepared saved seed subjects differ from their verified original')
        if rows.fetchone() is not None:
            raise MigrationError('Prepared saved seed contains extra subjects')
        rows = conn.execute('SELECT raw_version,policy_version,resolution_reason FROM '+preparation.POLICIES+
            ' WHERE source=? AND preparation_revision=? ORDER BY raw_version',(source,header['preparation_revision']))
        for _,policy in units[fields['subject_count']:]:
            known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?',(policy,)).fetchone()
            actual = rows.fetchone()
            if actual is None or tuple(actual) != (policy,policy if known else None,None if known else 'missing_policy'):
                raise MigrationError('Prepared saved seed policies differ from their verified original')
        if rows.fetchone() is not None:
            raise MigrationError('Prepared saved seed contains extra policies')
        return captured
    finally:
        conn.rollback()


def publish(conn,source,*,revision,clock=time.time):
    if type(revision) is not int or revision < 0:
        raise ValueError('Saved output publication requires a nonnegative revision')
    install(conn,source)
    captured = verify(conn,source,revision)
    try:
        conn.execute('BEGIN IMMEDIATE')
        job(source)._version(conn)
        job(source).validate(conn)
        validate(conn,source)
        if proof(conn,source,revision) != captured:
            raise MigrationError('Saved output changed after verification; reconcile before publication')
        generations,head,_ = tables(source)
        if not conn.execute('SELECT 1 FROM '+generations+' WHERE revision=?',(revision,)).fetchone():
            header = capture(conn,source)
            conn.execute('INSERT INTO '+generations+' VALUES (?,?,?,?)',
                (revision,source,header['preparation_revision'] if header is not None else None,clock()))
        conn.execute('INSERT INTO '+head+' VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1',
            (source,revision))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job',choices=tuple(preparation.SOURCES))
    parser.add_argument('database',type=Path)
    parser.add_argument('--revision',type=int,required=True)
    args = parser.parse_args(argv)
    source = preparation.SOURCES[args.job]
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw',uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            publish(conn,source,revision=args.revision)
            print(json.dumps({'source':source,'revision':selected(conn,source)}))
    except (MigrationError,sqlite3.Error,ValueError,OSError) as exc:
        parser.exit(1,f'Saved output publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
