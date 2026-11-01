"""Publish retained album occurrences in bounded batches, keeping legacy writers."""
from dataclasses import dataclass
import json
import time

from .. import album_tracklists
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import album_occurrences as preparation
from .selection import invalidators


NAME = 'album-occurrences'
PREFIX = 'published_album_occurrence_'
KINDS = {'tracks':(preparation.TRACKS, 'row_id', 'album_tracks'),
    'states':(preparation.STATES, 'album_id', 'album_fetch_state')}
RELATIONS = {'album_tracks':'read_tracks', 'album_fetch_state':'read_states',
    'album_track_links':'read_links', 'album_tracklists':'read_tracklists'}


def table(suffix):
    return PREFIX+suffix


@dataclass(frozen=True)
class Stage:
    id: int
    input_revision: int
    custody_revision: int
    owner: str
    after_key: int | None
    copied: int
    complete: int


def definitions():
    yield table('epoch'), f'CREATE TABLE {table("epoch")} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
    yield table('stages'), f'''CREATE TABLE {table('stages')} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,input_revision INTEGER NOT NULL CHECK(input_revision>=0),
        custody_revision INTEGER NOT NULL CHECK(custody_revision>=0),owner TEXT NOT NULL CHECK(owner IN ('tracks','states','groups')),
        after_key INTEGER,copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
    for kind, (source, identity, _) in KINDS.items():
        definition = next(sql for name, sql in preparation.DESTINATIONS if name == source)
        columns = definition.split('(', 1)[1].rsplit(')', 1)[0].replace(identity+' INTEGER PRIMARY KEY', identity+' INTEGER NOT NULL', 1)
        yield table(kind), (f'CREATE TABLE {table(kind)} (stage INTEGER NOT NULL REFERENCES {table("stages")}(id),'+
            columns+f',PRIMARY KEY(stage,{identity})) WITHOUT ROWID')
    yield table('tracks_album'), f'CREATE INDEX {table("tracks_album")} ON {table("tracks")}(stage,album_id,is_full,row_id)'
    yield table('groups'), f'''CREATE TABLE {table('groups')} (
        stage INTEGER NOT NULL REFERENCES {table('stages')}(id),album_id INTEGER NOT NULL,provider_id TEXT NOT NULL,
        member_count INTEGER NOT NULL CHECK(member_count>=0),order_status TEXT NOT NULL CHECK(order_status IN ('known','unknown','mixed')),
        status TEXT NOT NULL CHECK(status IN ('complete','partial')),expected_count INTEGER CHECK(expected_count>=0),fetched_at REAL,
        PRIMARY KEY(stage,album_id)) WITHOUT ROWID'''
    yield table('generations'), f'''CREATE TABLE {table('generations')} (
        revision INTEGER PRIMARY KEY REFERENCES {table('stages')}(id),input_revision INTEGER NOT NULL,published_at REAL NOT NULL)'''
    yield table('head'), f'''CREATE TABLE {table('head')} (
        name TEXT PRIMARY KEY CHECK(name={NAME!r}),revision INTEGER NOT NULL REFERENCES {table('generations')}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1))) WITHOUT ROWID'''
    for owner in ('tracks', 'states', 'groups', 'generations'):
        for operation in ('UPDATE', 'DELETE'):
            name = table(owner)+'_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table(owner)} '
                "BEGIN SELECT RAISE(ABORT,'Published album custody requires future cleanup'); END")
        if owner != 'generations':
            name = table(owner)+'_insert'
            yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{table(owner)} WHEN EXISTS('
                f'SELECT 1 FROM {table("stages")} WHERE id=NEW.stage AND complete=1) '
                "BEGIN SELECT RAISE(ABORT,'Completed album publication is retained'); END")
    for operation in ('UPDATE', 'DELETE'):
        name = table('stages')+'_'+operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table("stages")} WHEN OLD.complete=1 '
            "BEGIN SELECT RAISE(ABORT,'Completed album publication is retained'); END")
        name = preparation.ORIGINALS+'_retained_'+operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{preparation.ORIGINALS} '
            "BEGIN SELECT RAISE(ABORT,'Album originals require future cleanup'); END")
    for owner, _ in preparation.DESTINATIONS:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = owner+'_publication_epoch_'+operation.lower()
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} '
                f'BEGIN UPDATE {table("epoch")} SET revision=revision+1 WHERE id=1; END')
    yield from invalidators(table('head'), {NAME:preparation.JOB})
    name = table('input_changed')
    yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{preparation.JOB.state} '
        f'WHEN NEW.input_revision<>OLD.input_revision BEGIN UPDATE {table("head")} SET active=0 WHERE active=1; END')
    for operation in ('INSERT', 'UPDATE'):
        name = table('head')+'_evidence_'+operation.lower()
        changed = '' if operation == 'INSERT' else ' AND (OLD.active<>1 OR OLD.revision<>NEW.revision)'
        yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{table("head")} WHEN NEW.active=1'+changed+' BEGIN '
            "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; "
            'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')
    yield from views()


def views():
    active = f'SELECT revision FROM {table("head")} WHERE active=1'
    tracks, groups = (table(kind) for kind in ('tracks', 'groups'))
    yield table('read_tracks'), f'''CREATE VIEW {table('read_tracks')} AS WITH active AS ({active})
        SELECT t.row_id id,t.album_id,t.entry_id,CASE WHEN t.entry_id IS NULL THEN t.recording_id END recording_id,
            t.raw_identity_key legacy_track_key,t.position,t.disc_number,t.track_number,t.is_full,t.captured_source source
        FROM active h JOIN {tracks} t ON t.stage=h.revision JOIN {groups} g ON g.stage=t.stage AND g.album_id=t.album_id
        WHERE t.is_full=1 AND g.status='complete'
        UNION ALL
        SELECT t.row_id,t.album_id,t.entry_id,t.captured_recording_id,t.raw_identity_key,t.position,t.disc_number,t.track_number,0,t.captured_source
        FROM active h JOIN {tracks} t ON t.stage=h.revision WHERE t.is_full=0 AND NOT EXISTS(
            SELECT 1 FROM {tracks} f JOIN {groups} g ON g.stage=f.stage AND g.album_id=f.album_id
            WHERE f.stage=t.stage AND f.album_id=t.album_id AND f.is_full=1 AND f.entry_id=t.entry_id AND g.status='complete')
        UNION ALL SELECT * FROM main.album_tracks WHERE NOT EXISTS(SELECT 1 FROM active)'''
    yield table('read_states'), f'''CREATE VIEW {table('read_states')} AS WITH active AS ({active})
        SELECT g.album_id,g.fetched_at,1 complete,g.expected_count expected_track_count
        FROM active h JOIN {groups} g ON g.stage=h.revision WHERE g.status='complete'
        UNION ALL SELECT * FROM main.album_fetch_state WHERE NOT EXISTS(SELECT 1 FROM active)'''
    yield table('read_links'), f'''CREATE VIEW {table('read_links')} AS
        SELECT min(t.id) id,p.provider_id browse_id,e.recording_id,t.entry_id,e.video_id,min(t.source) source
        FROM {table('read_tracks')} t JOIN main.album_provider_ids p ON p.album_id=t.album_id AND p.provider='youtube'
        JOIN main.catalog_entries e ON e.id=t.entry_id GROUP BY p.provider_id,t.entry_id'''
    yield table('read_tracklists'), f'''CREATE VIEW {table('read_tracklists')} AS
        SELECT p.provider_id browse_id,a.title,a.artist,a.thumbnail,(SELECT json_group_array(k) FROM (
            SELECT coalesce(r.identity_key,t.legacy_track_key) k FROM {table('read_tracks')} t
            LEFT JOIN main.catalog_entries e ON e.id=t.entry_id LEFT JOIN main.recordings r ON r.id=coalesce(e.recording_id,t.recording_id)
            WHERE t.album_id=a.id AND t.is_full=1 ORDER BY t.position IS NULL,t.position,t.id)) track_keys
        FROM {table('read_states')} f JOIN main.albums a ON a.id=f.album_id
        JOIN main.album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' WHERE f.complete=1'''


def validate(conn):
    from ..migrations.current import _tokens
    # Frozen owner replacements retire source triggers, and versions 100/101
    # recreate only their canonical views. Native album owners take precedence
    # then; retained stages, originals and custody guards remain mandatory.
    retired = ({name for name, _ in (*invalidators(table('head'), {NAME:preparation.JOB}), *views())}
        if album_tracklists.available(conn) else set())
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None and name in retired:
            continue
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible album publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+table('epoch')+' WHERE id=1').fetchone():
        raise MigrationError('Album publication requires its custody revision')


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return
    validate(conn)
    if not album_tracklists.available(conn):
        preparation.JOB.validate(conn)
    if conn.execute('SELECT 1 FROM '+table('head')+' h LEFT JOIN '+table('generations')+
            ' g ON g.revision=h.revision LEFT JOIN '+table('stages')+' s ON s.id=g.revision '
            'WHERE g.revision IS NULL OR s.id IS NULL OR s.complete<>1 OR s.input_revision<>g.input_revision LIMIT 1').fetchone():
        raise MigrationError('Album selection requires its completed stage')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Album publication requires an idle connection')
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.JOB._version(conn)
        preparation.JOB.validate(conn)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
            validate(conn)
        else:
            for _, sql in definitions():
                conn.execute(sql)
            conn.execute('INSERT INTO '+table('epoch')+' VALUES (1,0)')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def proof(conn):
    progress = preparation.JOB.progress(conn)
    if (progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision
            or conn.execute('SELECT 1 FROM '+preparation.JOB.changes+' LIMIT 1').fetchone()):
        raise MigrationError('Album publication requires reconciled preparation')
    return progress, conn.execute('SELECT revision FROM '+table('epoch')+' WHERE id=1').fetchone()[0]


def stage(conn, identity):
    row = conn.execute('SELECT * FROM '+table('stages')+' WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise MigrationError('Album publication stage is missing')
    return Stage(*row)


def group(conn, identity, album):
    count, known, first, last = tuple(conn.execute('SELECT count(*),count(position),min(position),max(position) FROM '+table('tracks')+
        ' WHERE stage=? AND album_id=? AND is_full=1', (identity, album)).fetchone())
    state = conn.execute('SELECT * FROM '+table('states')+' WHERE stage=? AND album_id=?', (identity, album)).fetchone()
    if count == 0 and state is None:
        raise MigrationError('Album group has no captured full rows or state')
    provider = preparation.provider(conn, album)
    if provider is None:
        raise MigrationError('Album group requires its retained provider representation')
    order = 'known' if known == count else 'unknown' if not known else 'mixed'
    expected = state['expected_track_count'] if state else None
    complete = bool(count and state and state['complete'] and expected == count)
    if complete and order == 'known' and (first != 0 or last != count-1):
        raise MigrationError('Complete album group has incomplete captured order')
    return dict(album_id=album, provider_id=provider, member_count=count, order_status=order,
        status='complete' if complete else 'partial', expected_count=expected, fetched_at=state['fetched_at'] if state else None)


def group_keys(conn, identity, after=None, limit=33):
    return [row[0] for row in conn.execute('SELECT album_id FROM (SELECT album_id FROM '+table('tracks')+
        ' WHERE stage=? AND is_full=1 UNION SELECT album_id FROM '+table('states')+' WHERE stage=?)'+
        (' WHERE album_id>?' if after is not None else '')+' ORDER BY album_id LIMIT ?',
        (identity, identity, *((after,) if after is not None else ()), limit))]


def step(conn, *, batch_size=32):
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise ValueError('Album publication batches require 1 to 32 rows')
    install(conn)
    try:
        conn.execute('BEGIN')
        captured = proof(conn)
        row = conn.execute('SELECT * FROM '+table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
        current = Stage(*row) if row is not None and (row['input_revision'], row['custody_revision']) == (captured[0].input_revision, captured[1]) else None
        if current is not None and current.complete:
            return current
        kind, after = (current.owner, current.after_key) if current else ('tracks', None)
        if kind == 'groups':
            keys = group_keys(conn, current.id, after, batch_size+1)
            records = [group(conn, current.id, key) for key in keys[:batch_size]]
            cursor = keys[batch_size-1] if len(keys) >= batch_size else (keys[-1] if keys else after)
            more = len(keys) > batch_size
        else:
            source, identity, _ = KINDS[kind]
            # Text fields were bounded during preparation; bound again when
            # copying potentially damaged staging rows into permanent custody.
            columns = [preparation.clipped(row[1])+' AS '+row[1] if row[2] == 'TEXT' else row[1]
                for row in conn.execute('PRAGMA main.table_info('+source+')')]
            batch = conn.execute('SELECT '+','.join(columns)+' FROM '+source+
                (' WHERE '+identity+'>?' if after is not None else '')+' ORDER BY '+identity+' LIMIT ?',
                (*((after,) if after is not None else ()), batch_size+1)).fetchall()
            records = [dict(row) for row in batch[:batch_size]]
            if any(len(canonical(row)) > preparation.MAX_ROW_BYTES*2 for row in records):
                raise MigrationError('Prepared album row exceeds its custody byte budget')
            cursor = records[-1][identity] if records else after
            more = len(batch) > batch_size
    finally:
        conn.rollback()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if proof(conn) != captured or (current is not None and stage(conn, current.id) != current):
            raise MigrationError('Album publication inputs changed; stage reconciled preparation again')
        if current is None:
            identity = conn.execute('INSERT INTO '+table('stages')+
                "(input_revision,custody_revision,owner,after_key,copied,complete) VALUES (?,?,'tracks',NULL,0,0)",
                (captured[0].input_revision, captured[1])).lastrowid
            current = stage(conn, identity)
        for record in records:
            conn.execute('INSERT INTO '+table(kind)+'(stage,'+','.join(record)+') VALUES ('+
                ','.join('?' for _ in range(len(record)+1))+')', (current.id, *record.values()))
        next_kind = kind if more or kind == 'groups' else 'states' if kind == 'tracks' else 'groups'
        conn.execute('UPDATE '+table('stages')+' SET owner=?,after_key=?,copied=?,complete=? WHERE id=?',
            (next_kind, cursor if more else None, current.copied+len(records), int(kind == 'groups' and not more), current.id))
        result = stage(conn, current.id)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def verify(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Album publication verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        preparation.JOB._version(conn)
        preparation.JOB.validate(conn)
        validate(conn)
        captured, current = proof(conn), stage(conn, identity)
        if not current.complete or (current.input_revision, current.custody_revision) != (captured[0].input_revision, captured[1]):
            raise MigrationError('Album publication stage is incomplete or changed')
    finally:
        conn.rollback()
    total = 0
    for kind, (_, key, owner) in KINDS.items():
        after, count = None, 0
        while True:
            batch = conn.execute('SELECT * FROM '+table(kind)+' WHERE stage=?'+(' AND '+key+'>?' if after is not None else '')+
                ' ORDER BY '+key+' LIMIT 32', (identity, *((after,) if after is not None else ()))).fetchall()
            if not batch:
                break
            for row in batch:
                source = preparation.JOB._read(conn, owner, json.dumps([row[key]], separators=(',', ':')))
                if source is None or preparation.original(conn, owner, row[key], row['source_digest']) != dict(source):
                    raise MigrationError('Album publication original differs from its installed source')
                expected = preparation.describe(conn, owner, dict(source))
                if {column:row[column] for column in expected} != expected:
                    raise MigrationError('Album publication row differs from its original or references')
                if kind == 'tracks' and row['is_full'] and row['captured_recording_id'] is not None and row['captured_recording_id'] != row['recording_id']:
                    raise MigrationError('Album occurrence disagrees with its captured recording')
            after, count = batch[-1][key], count+len(batch)
        if conn.execute('SELECT count(*) FROM main.'+owner).fetchone()[0] != count:
            raise MigrationError('Album publication omits installed source rows')
        total += count
    after, count = None, 0
    while True:
        keys = group_keys(conn, identity, after, 32)
        if not keys:
            break
        for album in keys:
            actual = conn.execute('SELECT * FROM '+table('groups')+' WHERE stage=? AND album_id=?', (identity, album)).fetchone()
            expected = group(conn, identity, album)
            if actual is None or {key:actual[key] for key in expected} != expected:
                raise MigrationError('Album group differs from its captured occurrences or completeness')
        after, count = keys[-1], count+len(keys)
    if conn.execute('SELECT count(*) FROM '+table('groups')+' WHERE stage=?', (identity,)).fetchone()[0] != count or total+count != current.copied:
        raise MigrationError('Album publication has missing or extra groups')
    try:
        conn.execute('BEGIN')
        if proof(conn) != captured or stage(conn, identity) != current:
            raise MigrationError('Album inputs changed during verification; reconcile again')
    finally:
        conn.rollback()
    return captured, current


def publish(conn, *, identity, clock=time.time):
    if type(identity) is not int or identity <= 0:
        raise ValueError('Album publication requires a positive stage identity')
    install(conn)
    captured = verify(conn, identity)
    try:
        conn.execute('BEGIN IMMEDIATE')
        preparation.JOB._version(conn)
        preparation.JOB.validate(conn)
        validate(conn)
        if (proof(conn), stage(conn, identity)) != captured:
            raise MigrationError('Album inputs changed after verification; reconcile again')
        conn.execute('INSERT INTO '+table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING',
            (identity, captured[1].input_revision, clock()))
        conn.execute('INSERT INTO '+table('head')+' VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1', (NAME, identity))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def selected(conn):
    if album_tracklists.available(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


def relation(conn, owner):
    if owner not in RELATIONS:
        raise ValueError('Unsupported album read relation: '+owner)
    if album_tracklists.available(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return owner
    # The view selects or withdraws inside the consuming SQL snapshot. A writer
    # between this lookup and that query cannot leave it on a stale projection.
    return table(RELATIONS[owner])


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--publish-stage', type=int)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=32)
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
                print(json.dumps({'selected':selected(conn)}))
            else:
                for _ in range(args.batches):
                    current = step(conn, batch_size=args.batch_size)
                    if current.complete:
                        break
                print(json.dumps(asdict(current)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Album occurrence publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
