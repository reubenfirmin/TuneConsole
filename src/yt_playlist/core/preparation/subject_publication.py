"""Retain and select canonical listening subjects without replacing live writers.

Copy and summary batches run on an independent connection. Verification reads
the exact installed evidence outside a writer transaction; final selection checks
the same preparation and dependency revisions in one short transaction.
"""
from dataclasses import dataclass
import json
import time

from ..migrations.runner import MigrationError
from . import listening_subjects as preparation, listening_views


PREFIX = 'published_listening_subject_'
MAX_ROW_BYTES = preparation.MAX_ROW_BYTES*4
KINDS = ('events', 'observations', 'sources', 'calendar', 'receipts', 'calendars',
    'credits', 'recording_listening_summary', 'artist_listening_summary')
KEYS = {'events':('id',), 'observations':('id',), 'sources':('event_id', 'source'),
    'calendar':('event_id',), 'receipts':('id',), 'calendars':('account_id',),
    'credits':('entry_id', 'position'), 'recording_listening_summary':('account_id', 'recording_id'),
    'artist_listening_summary':('account_id', 'artist_id')}
DEPENDENCIES = ('play_event_sources', 'play_event_calendar', 'listening_capture_receipts',
    'listening_calendars', 'identities', 'recordings', 'catalog_entries', 'entry_artists',
    'artists', 'artist_redirects', 'recording_redirects')
READS = ('play_event_subjects', 'listening_observation_subjects', *listening_views.DERIVED,
    'recording_listening_summary', 'artist_listening_summary', 'trend_first_play')


def table(suffix):
    return PREFIX+suffix


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='listening_summary_state'").fetchone() is not None


@dataclass(frozen=True)
class Stage:
    id: int
    input_revision: int
    dependency_revision: int
    owner: str
    after_key: str | None
    copied: int
    complete: int


def definitions():
    yield table('epoch'), f'CREATE TABLE {table("epoch")} (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=0))'
    yield table('stages'), f'''CREATE TABLE {table('stages')} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,input_revision INTEGER NOT NULL,dependency_revision INTEGER NOT NULL,
        owner TEXT NOT NULL CHECK(owner IN {KINDS!r}),after_key TEXT CHECK(after_key IS NULL OR json_valid(after_key)),
        copied INTEGER NOT NULL CHECK(copied>=0),complete INTEGER NOT NULL CHECK(complete IN (0,1)))'''
    columns = {}
    for kind in ('events', 'observations'):
        sql = preparation.SCHEMA[preparation.DESTINATIONS[kind]]
        columns[kind] = sql.split('(', 1)[1].rsplit(')', 1)[0].replace('id INTEGER PRIMARY KEY', 'id INTEGER NOT NULL', 1)
    columns.update(sources='event_id INTEGER NOT NULL,source TEXT NOT NULL',
        calendar="event_id INTEGER NOT NULL,day TEXT NOT NULL CHECK(date(day) IS NOT NULL AND date(day)=day),timezone TEXT NOT NULL",
        receipts='id INTEGER NOT NULL,account_id INTEGER NOT NULL,captured_at REAL NOT NULL',
        calendars='account_id INTEGER NOT NULL,timezone TEXT NOT NULL,confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1))',
        credits='entry_id INTEGER NOT NULL,position INTEGER NOT NULL,recording_id INTEGER NOT NULL,artist_id INTEGER NOT NULL,source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot))')
    summary = ('account_id INTEGER NOT NULL,{subject} INTEGER NOT NULL,first_exact_play_at REAL,first_exact_day TEXT,'
        'first_observed_day TEXT,first_legacy_unknown_day TEXT,build_revision INTEGER NOT NULL,'
        'CHECK((first_exact_play_at IS NULL)=(first_exact_day IS NULL))')
    columns.update(recording_listening_summary=summary.format(subject='recording_id'),
        artist_listening_summary=summary.format(subject='artist_id'))
    for kind in KINDS:
        yield table(kind), (f'CREATE TABLE {table(kind)} (stage INTEGER NOT NULL REFERENCES {table("stages")}(id),'
            +columns[kind]+',PRIMARY KEY(stage,'+','.join(KEYS[kind])+')) WITHOUT ROWID')
    for kind, fields in (('events', 'account_id,recording_id,played_at,id'),
            ('observations', 'account_id,recording_id,day'), ('credits', 'recording_id,artist_id')):
        name = table(kind+'_subject')
        yield name, f'CREATE INDEX {name} ON {table(kind)}(stage,{fields})'
    yield table('generations'), f'''CREATE TABLE {table('generations')} (
        revision INTEGER PRIMARY KEY REFERENCES {table('stages')}(id),input_revision INTEGER NOT NULL,published_at REAL NOT NULL)'''
    yield table('head'), f'''CREATE TABLE {table('head')} (
        id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL REFERENCES {table('generations')}(revision),
        active INTEGER NOT NULL CHECK(active IN (0,1)))'''
    for kind in (*KINDS, 'generations'):
        for operation in ('UPDATE', 'DELETE'):
            name = table(kind+'_'+operation.lower())
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table(kind)} '
                "BEGIN SELECT RAISE(ABORT,'Published listening custody requires future cleanup'); END")
        if kind in KINDS:
            name = table(kind+'_insert')
            yield name, (f'CREATE TRIGGER {name} BEFORE INSERT ON main.{table(kind)} '
                f'WHEN EXISTS(SELECT 1 FROM {table("stages")} WHERE id=NEW.stage AND complete=1) '
                "BEGIN SELECT RAISE(ABORT,'Completed listening publication is retained'); END")
    for operation in ('UPDATE', 'DELETE'):
        name = table('stages_'+operation.lower())
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table("stages")} WHEN OLD.complete=1 '
            "BEGIN SELECT RAISE(ABORT,'Completed listening publication is retained'); END")
    # Source changes withdraw a selected generation immediately. Staging damage
    # also withdraws it, rather than leaving a previously checked proof selected.
    for owner in (*DEPENDENCIES, *preparation.DESTINATIONS.values()):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = table(owner+'_'+operation.lower())
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{owner} BEGIN '
                f'UPDATE {table("epoch")} SET revision=revision+1 WHERE id=1; '
                f'UPDATE {table("head")} SET active=0 WHERE active=1; END')
    name = table('input_changed')
    yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{preparation.STATE} '
        'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
        f'UPDATE {table("head")} SET active=0 WHERE active=1; END')
    for operation in ('INSERT', 'UPDATE'):
        name = table('head_evidence_'+operation.lower())
        changed = '' if operation == 'INSERT' else ' AND (OLD.active<>1 OR OLD.revision<>NEW.revision)'
        yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{table("head")} WHEN NEW.active=1'+changed+' BEGIN '
            "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; "
            'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')
    yield from views()


def views():
    for kind, name in (('events', 'play_event_subjects'), ('observations', 'listening_observation_subjects')):
        fields = ','.join('p.'+field for field in preparation.FIELDS[kind])
        yield table('stage_'+name), (f'CREATE VIEW {table("stage_"+name)} AS SELECT p.stage,{fields},'
            f'coalesce(r.identity_key,p.unresolved_subject_key) identity_key FROM {table(kind)} p '
            'LEFT JOIN main.recordings r ON r.id=p.recording_id')
        legacy = ','.join('p.identity_id AS account_id' if field == 'account_id' else
            'CASE WHEN p.recording_id IS NULL THEN p.identity_key END AS unresolved_subject_key' if field == 'unresolved_subject_key' else
            'NULL AS membership_ref_id' if field == 'membership_ref_id' else 'p.'+field for field in preparation.FIELDS[kind])
        yield table('read_'+name), f'''CREATE VIEW {table('read_'+name)} AS
            SELECT {fields},coalesce(r.identity_key,p.unresolved_subject_key) identity_key
            FROM {table('head')} h JOIN {table(kind)} p ON p.stage=h.revision
            LEFT JOIN main.recordings r ON r.id=p.recording_id WHERE h.active=1
            UNION ALL SELECT {legacy},p.identity_key FROM main.{preparation.SOURCES[kind]} p
            WHERE NOT EXISTS(SELECT 1 FROM {table('head')} WHERE active=1)'''
    for kind, source in (('sources', 'play_event_sources'), ('calendar', 'play_event_calendar')):
        fields = 'event_id,source' if kind == 'sources' else 'event_id,day,timezone'
        yield table('stage_'+kind), f'CREATE VIEW {table("stage_"+kind)} AS SELECT * FROM {table(kind)}'
        yield table('read_'+kind), f'''CREATE VIEW {table('read_'+kind)} AS
            SELECT {','.join('p.'+field for field in fields.split(','))} FROM {table('head')} h
            JOIN {table(kind)} p ON p.stage=h.revision WHERE h.active=1
            UNION ALL SELECT {fields} FROM main.{source}
            WHERE NOT EXISTS(SELECT 1 FROM {table('head')} WHERE active=1)'''
    for prefix, staged in (('stage_', True), ('read_', False)):
        relation = lambda name: table(prefix+name)
        for name, query in listening_views.definitions(relation, staged=staged):
            yield relation(name), f'CREATE VIEW {relation(name)} AS '+query
    for kind in KINDS[-2:]:
        fields = ','.join('s.'+field for field in (*KEYS[kind], 'first_exact_play_at', 'first_exact_day',
            'first_observed_day', 'first_legacy_unknown_day', 'build_revision'))
        yield table('read_'+kind), (f'CREATE VIEW {table("read_"+kind)} AS SELECT {fields} FROM {table("head")} h '
            f'JOIN {table(kind)} s ON s.stage=h.revision WHERE h.active=1')
    selected_first = listening_views.first_play(lambda name:table('read_'+name))
    yield table('read_trend_first_play'), f'''CREATE VIEW {table('read_trend_first_play')} AS
        SELECT * FROM ({selected_first}) WHERE EXISTS(SELECT 1 FROM {table('head')} WHERE active=1)
        UNION ALL SELECT * FROM main.trend_first_play WHERE NOT EXISTS(SELECT 1 FROM {table('head')} WHERE active=1)'''


def validate(conn):
    from ..migrations.current import _tokens
    retired = native(conn)
    for name, sql in definitions():
        # Frozen v100 removes all dependent views and replaces source owners.
        # Custody tables and their immutable guards survive and remain checked.
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if retired and (sql.startswith('CREATE VIEW') or name == table('input_changed') or
                any(name.startswith(table(owner+'_')) for owner in DEPENDENCIES)):
            continue
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible listening publication object: '+name)
    if not conn.execute('SELECT 1 FROM '+table('epoch')+' WHERE id=1').fetchone():
        raise MigrationError('Listening publication requires its dependency revision')


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return
    validate(conn)
    if not native(conn):
        preparation._validate_schema(conn)
    if conn.execute(f'''SELECT 1 FROM {table('head')} h LEFT JOIN {table('generations')} g ON g.revision=h.revision
            LEFT JOIN {table('stages')} s ON s.id=g.revision WHERE g.revision IS NULL OR s.complete<>1
            OR s.id IS NULL OR g.input_revision<>s.input_revision LIMIT 1''').fetchone():
        raise MigrationError('Listening selection requires its completed stage')


def install(conn):
    if conn.in_transaction:
        raise MigrationError('Listening publication requires an idle connection')
    def installed():
        preparation._version(conn)
        preparation._validate_schema(conn)
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
    progress = preparation.progress(conn)
    if (progress is None or progress.status != 'prepared' or progress.input_revision != progress.prepared_revision
            or conn.execute('SELECT 1 FROM '+preparation.CHANGES+' LIMIT 1').fetchone()):
        raise MigrationError('Listening publication requires reconciled preparation')
    return progress, conn.execute('SELECT revision FROM '+table('epoch')+' WHERE id=1').fetchone()[0]


def stage(conn, identity):
    row = conn.execute('SELECT * FROM '+table('stages')+' WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise MigrationError('Listening publication stage is missing')
    return Stage(*row)


def bounded_rows(conn, query, parameters=()):
    return preparation.bounded_rows(conn, query, parameters, byte_limit=MAX_ROW_BYTES)


def source_query(kind):
    if kind in preparation.DESTINATIONS:
        return 'SELECT * FROM main.'+preparation.DESTINATIONS[kind]
    if kind in ('sources', 'calendar'):
        return 'SELECT * FROM main.play_event_'+kind
    if kind in ('receipts', 'calendars'):
        source = 'listening_capture_receipts' if kind == 'receipts' else 'listening_calendars'
        fields = 'id,identity_id account_id,captured_at' if kind == 'receipts' else 'identity_id account_id,timezone,confirmed'
        return 'SELECT '+fields+' FROM main.'+source
    if kind == 'credits':
        return ("SELECT c.entry_id,c.position,e.recording_id,coalesce(r.artist_id,c.artist_id) artist_id,"
            "json_object('entry_id',c.entry_id,'position',c.position,'artist_id',c.artist_id,'credit',c.credit,"
            "'source',c.source,'recording_id',e.recording_id,'canonical_artist_id',coalesce(r.artist_id,c.artist_id)) source_snapshot "
            'FROM main.entry_artists c JOIN main.catalog_entries e ON e.id=c.entry_id '
            'LEFT JOIN main.artist_redirects r ON r.retired_id=c.artist_id')
    raise ValueError('Unsupported listening source: '+kind)


def summary_keys(conn, identity, kind, after, limit):
    if kind == 'recording_listening_summary':
        query = f'SELECT DISTINCT account_id,recording_id FROM {table("stage_eligible_listening_evidence")} WHERE stage=? AND recording_id IS NOT NULL'
    else:
        query = (f'SELECT DISTINCT s.account_id,c.artist_id FROM {table("recording_listening_summary")} s '
            f'JOIN {table("credits")} c ON c.stage=s.stage AND c.recording_id=s.recording_id WHERE s.stage=?')
    return conn.execute('SELECT * FROM ('+query+')'+
        (' WHERE ('+','.join(KEYS[kind])+')>(?,?)' if after is not None else '')+
        ' ORDER BY '+','.join(KEYS[kind])+' LIMIT ?', (identity, *(after or ()), limit)).fetchall()


def summary(conn, identity, kind, key):
    account, subject = key
    if kind == 'recording_listening_summary':
        evidence = table('stage_eligible_listening_evidence')
        exact = conn.execute(f'''SELECT observed_at,day FROM {evidence} WHERE stage=? AND account_id=?
            AND recording_id=? AND precision='exact_timestamp' ORDER BY observed_at,event_id LIMIT 1''',
            (identity, account, subject)).fetchone()
        coarse = conn.execute(f'''SELECT min(CASE WHEN precision='calendar_day' THEN day END),
            min(CASE WHEN precision='legacy_unknown' THEN day END) FROM {evidence}
            WHERE stage=? AND account_id=? AND recording_id=?''', (identity, account, subject)).fetchone()
    else:
        source = (f'FROM {table("recording_listening_summary")} s WHERE s.stage=? AND s.account_id=? '
            f'AND EXISTS(SELECT 1 FROM {table("credits")} c WHERE c.stage=s.stage '
            'AND c.recording_id=s.recording_id AND c.artist_id=?)')
        exact = conn.execute('SELECT first_exact_play_at,first_exact_day '+source+
            ' AND first_exact_play_at IS NOT NULL ORDER BY first_exact_play_at,recording_id LIMIT 1',
            (identity, account, subject)).fetchone()
        coarse = conn.execute('SELECT min(first_observed_day),min(first_legacy_unknown_day) '+source,
            (identity, account, subject)).fetchone()
    return dict(zip((*KEYS[kind], 'first_exact_play_at', 'first_exact_day', 'first_observed_day',
        'first_legacy_unknown_day', 'build_revision'), (*key, *(tuple(exact) if exact else (None, None)),
        *coarse, stage(conn, identity).input_revision)))


def batch(conn, identity, kind, after, limit):
    after = json.loads(after) if after is not None else None
    if kind in KINDS[-2:]:
        keys = summary_keys(conn, identity, kind, after, limit)
        return [summary(conn, identity, kind, tuple(key)) for key in keys]
    fields = KEYS[kind]
    key_sql = '('+','.join(fields)+')' if len(fields)>1 else fields[0]
    placeholders = '('+','.join('?' for _ in fields)+')' if len(fields)>1 else '?'
    return bounded_rows(conn, 'SELECT * FROM ('+source_query(kind)+')'+
        (' WHERE '+key_sql+'>'+placeholders if after is not None else '')+
        ' ORDER BY '+','.join(fields)+' LIMIT ?', (*(after or ()), limit))


def step(conn, *, batch_size=32):
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise ValueError('Listening publication batches require 1 to 32 rows')
    install(conn)
    try:
        conn.execute('BEGIN')
        captured = proof(conn)
        row = conn.execute('SELECT * FROM '+table('stages')+' ORDER BY id DESC LIMIT 1').fetchone()
        current = Stage(*row) if row and (row['input_revision'], row['dependency_revision']) == (captured[0].input_revision, captured[1]) else None
        if current and current.complete:
            return current
        kind, after = (current.owner, current.after_key) if current else (KINDS[0], None)
        records = batch(conn, current.id if current else None, kind, after, batch_size+1)
        more, records = len(records)>batch_size, records[:batch_size]
    finally:
        conn.rollback()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if proof(conn) != captured or (current and stage(conn, current.id) != current):
            raise MigrationError('Listening publication inputs changed; stage reconciled preparation again')
        if current is None:
            identity = conn.execute('INSERT INTO '+table('stages')+
                '(input_revision,dependency_revision,owner,copied,complete) VALUES (?,?,?,0,0)',
                (captured[0].input_revision, captured[1], KINDS[0])).lastrowid
            current = stage(conn, identity)
        for record in records:
            conn.execute('INSERT INTO '+table(kind)+'(stage,'+','.join(record)+') VALUES ('+
                ','.join('?' for _ in range(len(record)+1))+')', (current.id, *record.values()))
        next_kind = kind if more or kind == KINDS[-1] else KINDS[KINDS.index(kind)+1]
        cursor = json.dumps([records[-1][field] for field in KEYS[kind]], ensure_ascii=False) if more else None
        conn.execute('UPDATE '+table('stages')+' SET owner=?,after_key=?,copied=?,complete=? WHERE id=?',
            (next_kind, cursor, current.copied+len(records), int(kind == KINDS[-1] and not more), current.id))
        result = stage(conn, current.id)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def references(conn, identity, kind, value):
    """Retained stages cannot FK-lock live owners; prove their references here."""
    if kind in ('sources', 'calendar'):
        valid = conn.execute('SELECT 1 FROM '+table('events')+' WHERE stage=? AND id=?',
            (identity, value['event_id'])).fetchone()
    elif kind in ('receipts', 'calendars'):
        valid = conn.execute('SELECT 1 FROM main.identities WHERE id=?', (value['account_id'],)).fetchone()
    elif kind == 'credits':
        valid = conn.execute('SELECT 1 FROM main.recordings r JOIN main.artists a ON a.id=? WHERE r.id=?',
            (value['artist_id'], value['recording_id'])).fetchone()
    elif kind == 'observations':
        valid = conn.execute('SELECT (? IS NULL OR (date(?) IS NOT NULL AND date(?)=?)) '
            'AND (? IS NULL OR (date(?) IS NOT NULL AND date(?)=?))',
            (value['source_day'],)*4+(value['day'],)*4).fetchone()[0]
    else:
        return
    if not valid:
        raise MigrationError('Listening publication has an invalid reference or date: '+kind)


def verify(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Listening verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        preparation._version(conn)
        preparation._validate_schema(conn)
        validate(conn)
        captured, current = proof(conn), stage(conn, identity)
        if not current.complete or (current.input_revision, current.dependency_revision) != (captured[0].input_revision, captured[1]):
            raise MigrationError('Listening publication stage is incomplete or changed')
    finally:
        conn.rollback()
    count = 0
    for kind in KINDS:
        after, kind_count = None, 0
        while True:
            # Each batch uses a short read snapshot; writers remain free between
            # batches and the final revision fence rejects any intervening edit.
            try:
                conn.execute('BEGIN')
                if proof(conn) != captured:
                    raise MigrationError('Listening inputs changed during verification; reconcile again')
                expected = batch(conn, identity, kind, after, 32)
                for value in expected:
                    key = tuple(value[field] for field in KEYS[kind])
                    actual = conn.execute('SELECT * FROM '+table(kind)+' WHERE stage=? AND '+
                        ' AND '.join(field+'=?' for field in KEYS[kind]), (identity, *key)).fetchone()
                    if actual is None or {name:actual[name] for name in value} != value:
                        raise MigrationError('Listening publication differs from its current evidence: '+kind)
                    references(conn, identity, kind, value)
                    if kind in preparation.SOURCES:
                        source = bounded_rows(conn, 'SELECT * FROM main.'+preparation.SOURCES[kind]+' WHERE id=?', (value['id'],))
                        if not source or preparation.describe(conn, kind, source[0]) != value:
                            raise MigrationError('Listening publication original differs from its installed source')
                if not expected:
                    break
                after = json.dumps([expected[-1][field] for field in KEYS[kind]], ensure_ascii=False)
                count += len(expected)
                kind_count += len(expected)
            finally:
                conn.rollback()
        if conn.execute('SELECT count(*) FROM '+table(kind)+' WHERE stage=?', (identity,)).fetchone()[0] != kind_count:
            raise MigrationError('Listening publication has missing or extra rows: '+kind)
        if kind in preparation.SOURCES and conn.execute('SELECT count(*) FROM main.'+preparation.SOURCES[kind]).fetchone()[0] != kind_count:
            raise MigrationError('Listening publication omits installed source rows: '+kind)
        if kind == 'credits' and conn.execute('SELECT count(*) FROM main.entry_artists').fetchone()[0] != kind_count:
            raise MigrationError('Listening publication omits installed artist credits')
    if count != current.copied:
        raise MigrationError('Listening publication has an incorrect retained row count')
    try:
        conn.execute('BEGIN')
        if (proof(conn), stage(conn, identity)) != (captured, current):
            raise MigrationError('Listening inputs changed during verification; reconcile again')
    finally:
        conn.rollback()
    return captured, current


def publish(conn, *, identity, clock=time.time):
    if type(identity) is not int or identity<=0:
        raise ValueError('Listening publication requires a positive stage identity')
    install(conn)
    captured = verify(conn, identity)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if (proof(conn), stage(conn, identity)) != captured:
            raise MigrationError('Listening inputs changed after verification; reconcile again')
        conn.execute('INSERT INTO '+table('generations')+' VALUES (?,?,?) ON CONFLICT DO NOTHING',
            (identity, captured[1].input_revision, clock()))
        conn.execute('INSERT INTO '+table('head')+' VALUES (1,?,1) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1', (identity,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def selected(conn):
    if native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


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
    if not 1<=args.batches<=128:
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
        parser.exit(1, f'Listening subject publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
