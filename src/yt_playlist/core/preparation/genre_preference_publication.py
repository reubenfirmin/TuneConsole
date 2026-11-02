"""Select verified enabled choices and disabled defaults without retiring sources."""
from dataclasses import asdict, dataclass
import hashlib
import json
import time

from .. import genre_identity
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from ...util import genre_catalog
from . import bounded, genre_preferences as preparation


PREFIX = 'published_genre_preferences_'
MAX_BYTES = 64*1024
FIELDS = ('kind', 'source_key', 'position', 'raw_label', 'genre_key', 'proposed_key',
          'disposition', 'enabled', 'source_digest', 'source_snapshot')


def table(name):
    return PREFIX+name


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='genre_preferences'").fetchone() is not None


@dataclass(frozen=True)
class Stage:
    id: int
    proof: str
    baseline: str
    after_kind: str | None
    after_key: str | None
    copied: int
    complete: int


def definitions():
    yield table('epoch'), f'CREATE TABLE {table("epoch")} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
    yield table('stages'), f'''CREATE TABLE {table('stages')} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,proof TEXT NOT NULL CHECK(json_valid(proof)),
        baseline TEXT NOT NULL CHECK(json_valid(baseline) AND length(CAST(baseline AS BLOB))<={MAX_BYTES}),
        after_kind TEXT,after_key TEXT CHECK(after_key IS NULL OR json_valid(after_key)),
        copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
    yield table('rows'), f'''CREATE TABLE {table('rows')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),
        kind TEXT NOT NULL CHECK(kind IN ('choice','default')),source_key TEXT NOT NULL CHECK(json_valid(source_key)),
        position INTEGER CHECK(position>=0),raw_label TEXT NOT NULL,genre_key TEXT REFERENCES genre_catalog(key),
        proposed_key TEXT,disposition TEXT NOT NULL CHECK(disposition IN ('resolved','requires_custom','blank','unknown_genre','ambiguous_genre')),
        enabled INTEGER CHECK(enabled IN (0,1)),source_digest TEXT,source_snapshot TEXT CHECK(source_snapshot IS NULL OR json_valid(source_snapshot)),
        PRIMARY KEY(stage,kind,source_key),CHECK((genre_key IS NOT NULL)=(disposition='resolved')),
        CHECK((proposed_key IS NOT NULL)=(disposition='requires_custom')),
        CHECK((kind='choice' AND position IS NULL AND enabled IS 1 AND source_digest IS NOT NULL AND length(source_digest)=64 AND source_snapshot IS NOT NULL)
            OR (kind='default' AND position IS NOT NULL AND (enabled=0 OR enabled IS NULL) AND source_digest IS NULL AND source_snapshot IS NULL))) WITHOUT ROWID'''
    yield table('generations'), f'''CREATE TABLE {table('generations')} (
        revision INTEGER PRIMARY KEY REFERENCES {table('stages')}(id),proof TEXT NOT NULL CHECK(json_valid(proof)),published_at REAL NOT NULL)'''
    yield table('head'), f'''CREATE TABLE {table('head')} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {table('generations')}(revision),active INTEGER NOT NULL CHECK(active IN (0,1)))'''
    yield table('preferences'), f'''CREATE VIEW {table('preferences')} AS
        SELECT stage,genre_key,max(enabled) enabled,CASE WHEN max(enabled)=1 THEN 'legacy' ELSE 'default' END origin
        FROM {table('rows')} WHERE genre_key IS NOT NULL AND enabled IS NOT NULL GROUP BY stage,genre_key'''
    for owner in (table('rows'), table('generations'), table('stages'),
                  'prepared_genre_choice_originals', preparation.HEAD, preparation.ITEMS):
        for operation in ('UPDATE', 'DELETE'):
            name = table(owner+'_'+operation.lower())
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{owner} '+
                ('WHEN OLD.complete=1 ' if owner == table('stages') else '')+
                "BEGIN SELECT RAISE(ABORT,'Retained genre preferences require future cleanup'); END")
    name = table('rows_insert')
    yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{table("rows")} '
        f'WHEN EXISTS(SELECT 1 FROM {table("stages")} WHERE id=NEW.stage AND complete=1) '
        "BEGIN SELECT RAISE(ABORT,'Completed genre preferences are retained'); END")
    for owner in ('prepared_genre_choices', preparation.HEAD, preparation.ITEMS):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = table(owner+'_'+operation.lower()+'_changed')
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN '
                f'UPDATE {table("epoch")} SET revision=revision+1 WHERE id=1; '
                f'UPDATE {table("head")} SET active=0 WHERE active=1; END')
    for job in (preparation.CHOICES, preparation.DEFAULT_DEFINITION):
        name = table(job.name+'_changed')
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{job.state} '
            'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
            f'UPDATE {table("head")} SET active=0 WHERE active=1; END')


def validate(conn):
    from ..schema_contract import tokens as _tokens
    from ..entity_names import predecessor_definition as transform
    renamed = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(transform(sql) if renamed else sql):
            raise MigrationError('Incompatible genre preference publication: '+name)
    if not conn.execute('SELECT 1 FROM '+table('epoch')+' WHERE id=1').fetchone():
        raise MigrationError('Genre preferences require their custody revision')


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return
    validate(conn)
    if not native(conn):
        for job in (preparation.CHOICES, preparation.DEFAULT_DEFINITION):
            job.validate(conn)
    if conn.execute('SELECT 1 FROM '+table('head')+' h LEFT JOIN '+table('generations')+
        ' g ON g.revision=h.revision LEFT JOIN '+table('stages')+
        ' s ON s.id=g.revision WHERE s.id IS NULL OR s.complete<>1 OR g.proof<>s.proof LIMIT 1').fetchone():
        raise MigrationError('Genre preferences require a completed selection')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Genre preference publication requires an idle connection')
    def installed():
        for job in (preparation.CHOICES, preparation.DEFAULT_DEFINITION):
            job._version(conn)
            job.validate(conn)
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
    values = []
    for job in (preparation.CHOICES, preparation.DEFAULT_DEFINITION):
        current = job.progress(conn)
        if (current is None or current.status != 'prepared' or current.input_revision != current.prepared_revision
                or conn.execute('SELECT 1 FROM '+job.changes+' LIMIT 1').fetchone()):
            raise MigrationError('Genre preferences require reconciled enabled choices and defaults')
        values.append(asdict(current))
    values.append(conn.execute('SELECT revision FROM '+table('epoch')+' WHERE id=1').fetchone()[0])
    return canonical(values).decode()


def baseline(conn):
    generation = preparation.DEFAULTS.progress(conn).input_revision
    row = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?', (generation,)).fetchone()
    if row is None:
        raise MigrationError('Genre preferences require a prepared baseline')
    return canonical(dict(row)).decode()


def stage(conn, identity):
    row = conn.execute('SELECT * FROM '+table('stages')+' WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise MigrationError('Genre preference stage is missing')
    return Stage(*row)


def latest(conn):
    row = conn.execute('SELECT * FROM '+table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
    return Stage(*row) if row else None


def batch(conn, current, size):
    generation = json.loads(current.baseline)['generation']
    query = ("SELECT 'choice' kind,c.source_key,NULL position,c.raw_label,c.genre_key,c.proposed_key,c.disposition,"
        '1 enabled,c.source_digest,o.source_snapshot FROM prepared_genre_choices c '
        'JOIN prepared_genre_choice_originals o ON o.source_key=c.source_key AND o.digest=c.source_digest '
        "UNION ALL SELECT 'default',json_array(position),position,raw_label,genre_key,NULL,disposition,"
        'proposed_enabled,NULL,NULL FROM '+preparation.ITEMS+' WHERE generation=?')
    parameters = (generation,)
    query = 'SELECT * FROM ('+query+')'
    if current.after_kind is not None:
        query += ' WHERE (kind,source_key)>(?,?)'
        parameters += (current.after_kind, current.after_key)
    return bounded.rows(conn, query+' ORDER BY kind,source_key LIMIT ?', (*parameters, size),
        byte_limit=MAX_BYTES, message='Genre preference exceeds its publication row budget')


def step(conn, *, batch_size=16):
    if type(batch_size) is not int or not 1<=batch_size<=16:
        raise ValueError('Genre preferences require 1 to 16 rows per batch')
    install(conn)
    try:
        conn.execute('BEGIN')
        captured, previous = proof(conn), latest(conn)
        current = previous if previous and previous.proof == captured else Stage(0, captured, baseline(conn), None, None, 0, 0)
        if current.complete:
            return current
        rows = batch(conn, current, batch_size+1)
        more, rows = len(rows)>batch_size, rows[:batch_size]
        for row in rows:
            if row['kind'] == 'choice' and row['disposition'] != 'resolved':
                raise MigrationError('Enabled genre '+repr(row['raw_label'])+' requires explicit custom admission or ambiguity repair before publication')
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        if proof(conn) != captured or latest(conn) != previous:
            raise MigrationError('Genre preferences changed before staging; reconcile again')
        if not current.id:
            identity = conn.execute('INSERT INTO '+table('stages')+
                '(proof,baseline,copied,complete) VALUES (?,?,0,0)', (captured, current.baseline)).lastrowid
            current = stage(conn, identity)
        for row in rows:
            conn.execute('INSERT INTO '+table('rows')+'(stage,'+','.join(row)+') VALUES ('+
                ','.join('?' for _ in range(len(row)+1))+')', (current.id, *row.values()))
        conn.execute('UPDATE '+table('stages')+' SET after_kind=?,after_key=?,copied=?,complete=? WHERE id=?',
            (rows[-1]['kind'] if rows else current.after_kind, rows[-1]['source_key'] if rows else current.after_key,
             current.copied+len(rows), int(not more), current.id))
        result = stage(conn, current.id)
        conn.commit()
        return result
    finally:
        conn.rollback()


def installed_baseline(conn, header):
    source = conn.execute("SELECT substr(CAST(value AS BLOB),1,?) FROM main.settings WHERE key='genre_defaults'",
                          (preparation.MAX_BYTES+1,)).fetchone()
    encoded = bytes(source[0]) if source and source[0] is not None else None
    if source is None:
        encoded = json.dumps(genre_catalog.legacy_names()).encode()
    seeded = conn.execute("SELECT coalesce(substr(CAST(value AS BLOB),1,?),X''),value IS NULL FROM main.settings WHERE key='genres_seeded'",
                          (preparation.MAX_SEEDED_BYTES+1,)).fetchone()
    seed_value = bytes(seeded[0]).decode() if seeded and not seeded[1] else None
    if (encoded is None or len(encoded)>preparation.MAX_BYTES or hashlib.sha256(encoded).hexdigest() != header['digest']
            or header['baseline_origin'] != ('saved' if source else 'legacy_defaults')
            or header['seeded_present'] != int(seeded is not None) or header['seeded_value'] != seed_value
            or header['apply_defaults'] != int(bool(seed_value) or bool(conn.execute('SELECT 1 FROM main.genre_whitelist LIMIT 1').fetchone()))):
        raise MigrationError('Genre baseline differs from its installed original')
    return encoded


def verify_baseline(conn, header):
    raw = preparation.read_baseline(header)
    if installed_baseline(conn, header) != raw.encode():
        raise MigrationError('Genre baseline file differs from its original')
    labels = json.loads(raw)
    if not isinstance(labels, list) or any(not isinstance(label,str) for label in labels) or len(labels) != header['labels_count']:
        raise MigrationError('Genre baseline has incorrect ordered labels')
    return labels


def verify(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Genre preference verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        validate_installed(conn)
        captured, current = proof(conn), stage(conn, identity)
        if not current.complete or current.proof != captured or current.baseline != baseline(conn):
            raise MigrationError('Genre preference stage is incomplete or changed')
        header = json.loads(current.baseline)
    finally:
        conn.rollback()
    # Recovery file I/O and JSON decoding run without any SQL reservation.
    labels = verify_baseline(conn, header)
    count, cursor = 0, Stage(current.id, current.proof, current.baseline, None, None, 0, 0)
    while True:
        try:
            conn.execute('BEGIN')
            if proof(conn) != captured:
                raise MigrationError('Genre preferences changed during verification; reconcile again')
            rows = batch(conn, cursor, 16)
            for row in rows:
                if row['kind'] == 'choice':
                    source = preparation.CHOICES._read(conn, 'genre_whitelist', row['source_key'])
                    if source is None:
                        raise MigrationError('Genre choice has no installed original')
                    values, snapshot = preparation.describe_choice(conn, row['source_key'], source)
                    expected = dict(zip(FIELDS, ('choice', *values[:1], None, *values[1:5], 1, values[5], snapshot)))
                    if expected['disposition'] != 'resolved':
                        raise MigrationError('Enabled genre requires custom admission or ambiguity repair')
                else:
                    position = row['position']
                    if type(position) is not int or not 0<=position<len(labels):
                        raise MigrationError('Genre default position is outside its baseline')
                    label = labels[position]
                    key, disposition = preparation.resolve(conn, label)
                    expected = dict(zip(FIELDS, ('default', json.dumps([position], separators=(',', ':')), position,
                        label, key, None, disposition, 0 if header['apply_defaults'] else None, None, None)))
                actual = conn.execute('SELECT '+','.join(FIELDS)+' FROM '+table('rows')+
                    ' WHERE stage=? AND kind=? AND source_key=?', (identity, row['kind'], row['source_key'])).fetchone()
                if row != expected or actual is None or dict(actual) != expected:
                    raise MigrationError('Genre preference differs from prepared or original values')
            if not rows:
                break
            count += len(rows)
            cursor = Stage(current.id, current.proof, current.baseline, rows[-1]['kind'], rows[-1]['source_key'], count, 0)
        finally:
            conn.rollback()
    expected_count = conn.execute('SELECT count(*) FROM main.genre_whitelist').fetchone()[0]+len(labels)
    if (count != expected_count or current.copied != count
            or conn.execute('SELECT count(*) FROM '+table('rows')+' WHERE stage=?', (identity,)).fetchone()[0] != count
            or conn.execute('SELECT count(*) FROM prepared_genre_choices').fetchone()[0]+conn.execute(
                'SELECT count(*) FROM '+preparation.ITEMS+' WHERE generation=?', (header['generation'],)).fetchone()[0] != count):
        raise MigrationError('Genre preferences have missing or extra inputs')
    try:
        conn.execute('BEGIN')
        if (proof(conn), stage(conn, identity)) != (captured, current):
            raise MigrationError('Genre preferences changed during verification; reconcile again')
    finally:
        conn.rollback()
    return captured, current


def publish(conn, *, identity, clock=time.time):
    if type(identity) is not int or identity<=0:
        raise ValueError('Genre preferences require a positive stage identity')
    install(conn)
    captured = verify(conn, identity)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if (proof(conn), stage(conn, identity)) != captured:
            raise MigrationError('Genre preferences changed after verification; reconcile again')
        conn.execute('INSERT INTO '+table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING', (identity, captured[0], clock()))
        conn.execute('INSERT INTO '+table('head')+' VALUES (1,?,1) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1', (identity,))
        conn.commit()
    finally:
        conn.rollback()


def selected(conn):
    if native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


def relation(conn):
    if native(conn):
        return 'main.genre_preferences'
    identity = selected(conn)
    return '(SELECT genre_key,enabled,origin FROM main.'+table('preferences')+' WHERE stage='+str(identity)+')' if identity is not None else None


def repair_baseline(conn):
    """Restore the exact captured file only while its source proof still holds."""
    from ..policy_documents import _publish_encoded
    install(conn)
    try:
        conn.execute('BEGIN')
        captured, header_text = proof(conn), baseline(conn)
        header = json.loads(header_text)
        encoded = installed_baseline(conn, header)
    finally:
        conn.rollback()
    filename = _publish_encoded(preparation.directory(conn), encoded, header['digest'])
    if str(filename) != header['filename']:
        raise MigrationError('Repaired genre baseline has an unexpected file owner')
    try:
        conn.execute('BEGIN')
        if (proof(conn), baseline(conn)) != (captured, header_text):
            raise MigrationError('Genre defaults changed during repair; reconcile again')
    finally:
        conn.rollback()
    return filename


def admit_custom(conn, *, batch_size=16):
    """Create only identities explicitly requested by retained enabled labels."""
    if type(batch_size) is not int or not 1<=batch_size<=16:
        raise ValueError('Custom genre admission requires 1 to 16 choices')
    if conn.in_transaction:
        raise MigrationError('Custom genre admission requires an idle connection')
    try:
        conn.execute('BEGIN')
        preparation.CHOICES._version(conn)
        preparation.CHOICES.validate(conn)
        current = preparation.CHOICES.progress(conn)
        if current is None or current.status != 'prepared' or current.input_revision != current.prepared_revision:
            raise MigrationError('Custom genre admission requires reconciled enabled choices')
        rows = bounded.rows(conn, "SELECT * FROM prepared_genre_choices WHERE disposition='requires_custom' ORDER BY source_key LIMIT ?",
            (batch_size,), byte_limit=MAX_BYTES, message='Custom genre choice exceeds its byte budget')
        for row in rows:
            source = preparation.CHOICES._read(conn, 'genre_whitelist', row['source_key'])
            if source is None or preparation.describe_choice(conn, row['source_key'], source)[0] != tuple(row.values()):
                raise MigrationError('Custom genre intent differs from its installed original')
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        if preparation.CHOICES.progress(conn) != current:
            raise MigrationError('Enabled choices changed before custom admission; reconcile again')
        for row in rows:
            genre_identity.add_custom(conn, row['raw_label'])
        conn.commit()
        return len(rows)
    finally:
        conn.rollback()


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--admit-custom', action='store_true')
    action.add_argument('--repair-baseline', action='store_true')
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
            if args.repair_baseline:
                print(json.dumps({'repaired':str(repair_baseline(conn))}))
            elif args.admit_custom:
                print(json.dumps({'admitted':admit_custom(conn, batch_size=args.batch_size), 'next':'Reconcile enabled/default preparation again.'}))
            elif args.publish_stage is not None:
                publish(conn, identity=args.publish_stage)
                print(json.dumps({'selected':selected(conn)}))
            else:
                for _ in range(args.batches):
                    current = step(conn, batch_size=args.batch_size)
                    if current.complete:
                        break
                print(json.dumps(asdict(current)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Genre preference publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
