"""Prepare listening subjects in bounded transactions while legacy writers run.

The installed event/observation tables remain authoritative. A coalescing change
journal covers inserts, updates, deletions and reused IDs behind the copy cursor.
Committing an empty journal publishes a preparation revision, not a runtime owner
change. Later writes invalidate that revision and are reconciled on the next run.
No startup worker invokes this job. Subject publication is a separate checked
operation; #626 owns eventual source retirement.
"""
from dataclasses import dataclass
import json
import sqlite3
import time

from ..migrations.runner import MigrationError, history, validate_version


STATE = 'listening_subject_preparation'
CHANGES = 'listening_subject_changes'
SOURCES = {'events': 'play_events', 'observations': 'listening_observations'}
DESTINATIONS = {'events': 'prepared_listening_events', 'observations': 'prepared_listening_observations'}
MAX_ROW_BYTES = 65536
MAX_BATCH_ROWS = 32
FIELDS = {
    'events': ('id', 'account_id', 'raw_identity_key', 'recording_id', 'unresolved_subject_key',
               'video_id', 'played_at', 'playlist_ytm_id', 'membership_ref_id', 'like_status', 'is_generated'),
    'observations': ('id', 'account_id', 'raw_identity_key', 'recording_id', 'unresolved_subject_key',
                     'source_day', 'day', 'source', 'timezone', 'precision', 'first_observed_at',
                     'last_observed_at', 'capture_id', 'capture_item_id'),
}
SCHEMA = {
    STATE: f'''CREATE TABLE {STATE} (
        id INTEGER PRIMARY KEY CHECK(id=1), source_version INTEGER NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('events','observations','changes')),
        cursor INTEGER, events_highwater INTEGER, observations_highwater INTEGER,
        status TEXT NOT NULL CHECK(status IN ('copying','reconciling','prepared','failed')),
        input_revision INTEGER NOT NULL DEFAULT 0 CHECK(input_revision>=0), prepared_revision INTEGER,
        processed INTEGER NOT NULL DEFAULT 0 CHECK(processed>=0), updated_at REAL NOT NULL, error TEXT,
        CHECK((status='failed')=(error IS NOT NULL)),
        CHECK(status<>'prepared' OR (prepared_revision IS NOT NULL AND prepared_revision=input_revision))
    )''',
    CHANGES: f'''CREATE TABLE {CHANGES} (
        kind TEXT NOT NULL CHECK(kind IN ('events','observations')),
        source_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(kind,source_id)
    ) WITHOUT ROWID''',
    DESTINATIONS['events']: '''CREATE TABLE prepared_listening_events (
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, raw_identity_key TEXT,
        recording_id INTEGER, unresolved_subject_key TEXT, video_id TEXT,
        played_at REAL NOT NULL, playlist_ytm_id TEXT, membership_ref_id INTEGER,
        like_status TEXT, is_generated INTEGER CHECK(is_generated IN (0,1)),
        source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
        CHECK((recording_id IS NOT NULL AND unresolved_subject_key IS NULL)
           OR (recording_id IS NULL AND unresolved_subject_key IS NOT NULL))
    )''',
    DESTINATIONS['observations']: '''CREATE TABLE prepared_listening_observations (
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, raw_identity_key TEXT,
        recording_id INTEGER, unresolved_subject_key TEXT, source_day TEXT, day TEXT,
        source TEXT NOT NULL, timezone TEXT, precision TEXT NOT NULL,
        first_observed_at REAL NOT NULL, last_observed_at REAL NOT NULL,
        capture_id INTEGER, capture_item_id INTEGER,
        source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
        CHECK((recording_id IS NOT NULL AND unresolved_subject_key IS NULL)
           OR (recording_id IS NULL AND unresolved_subject_key IS NOT NULL)),
        CHECK((precision='calendar_day' AND timezone IS NOT NULL AND raw_identity_key IS NOT NULL
               AND source_day IS NOT NULL AND capture_id IS NULL AND capture_item_id IS NULL)
           OR (precision='legacy_unknown' AND timezone IS NULL AND capture_id IS NOT NULL AND capture_item_id IS NOT NULL))
    )''',
}


def _triggers():
    for kind, source in SOURCES.items():
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'prepare_listening_{kind}_{operation.lower()}'
            sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
            dirty = ''.join(f"INSERT INTO {CHANGES}(kind,source_id,revision) "
                f"SELECT '{kind}',{side}.id,input_revision FROM {STATE} WHERE id=1 "
                'ON CONFLICT(kind,source_id) DO UPDATE SET revision=excluded.revision;' for side in sides)
            yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.{source} BEGIN '
                f'UPDATE {STATE} SET input_revision=input_revision+1,prepared_revision=NULL,'
                "phase=CASE WHEN status='prepared' THEN 'changes' ELSE phase END,"
                "status=CASE WHEN status='prepared' THEN 'reconciling' ELSE status END WHERE id=1;"
                + dirty + ' END')
    # A recording-key edit invalidates the equality proof used when copying a
    # resolved legacy subject. Resetting the cursor is O(1) for the live writer;
    # the explicit job repeats its bounded scan and checks the installed values.
    name = 'prepare_listening_recording_key'
    yield name, (f'CREATE TRIGGER {name} AFTER UPDATE OF identity_key ON main.recordings '
        'WHEN OLD.identity_key IS NOT NEW.identity_key BEGIN '
        f'UPDATE {STATE} SET input_revision=input_revision+1,prepared_revision=NULL,'
        "phase='events',cursor=NULL,events_highwater=(SELECT max(id) FROM play_events),"
        'observations_highwater=(SELECT max(id) FROM listening_observations),'
        "status=CASE WHEN status='failed' THEN status ELSE 'copying' END WHERE id=1; END")


@dataclass(frozen=True)
class Progress:
    status: str
    phase: str
    cursor: int | None
    processed: int
    input_revision: int
    prepared_revision: int | None
    error: str | None


def progress(conn):
    row = conn.execute(f'SELECT status,phase,cursor,processed,input_revision,prepared_revision,error FROM {STATE} WHERE id=1').fetchone()
    return Progress(*row) if row is not None else None


def bounded_rows(conn, query, parameters=(), *, byte_limit=MAX_ROW_BYTES):
    """Reject large rows in SQLite before transferring their payload to Python."""
    from .bounded import rows
    return rows(conn, query, parameters, byte_limit=byte_limit, message='Listening row exceeds its byte budget')


def _version(conn):
    from ..migrations.registry import MIGRATIONS
    version = history(conn, MIGRATIONS)
    if not 79 <= version < 100:
        raise MigrationError('Listening preparation requires a genuine installed layout from 79 through 99')
    return version


def _validate_schema(conn):
    from ..migrations.current import _tokens
    for name, sql in (*SCHEMA.items(), *_triggers()):
        actual = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError('Incompatible listening preparation object: ' + name)


def install(conn, *, clock=time.time):
    """Install additive staging/journal DDL without copying a source row."""
    if conn.in_transaction:
        raise MigrationError('Cannot prepare listening with an outstanding caller transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        version = _version(conn)
        validate_version(conn, version, foreign_keys=False, data=False)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (STATE,)).fetchone():
            _validate_schema(conn)
        else:
            for sql in SCHEMA.values():
                conn.execute(sql)
            highwaters = [conn.execute(f'SELECT max(id) FROM main.{source}').fetchone()[0]
                          for source in SOURCES.values()]
            conn.execute(f'INSERT INTO {STATE}(id,source_version,phase,status,updated_at,events_highwater,observations_highwater) '
                         "VALUES (1,?,'events','copying',?,?,?)", (version, clock(), *highwaters))
            for _, sql in _triggers():
                conn.execute(sql)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return progress(conn)


def describe(conn, kind, row):
    """Derive a typed subject without changing either owner."""
    source = dict(row)
    recording = source['recording_id']
    if recording is not None:
        known = conn.execute('SELECT identity_key FROM main.recordings WHERE id=?', (recording,)).fetchone()
        if known is None or known[0] != source['identity_key']:
            raise MigrationError(f'Listening subject disagrees with recording: {SOURCES[kind]} {source["id"]}')
    if not conn.execute('SELECT 1 FROM main.identities WHERE id=?', (source['identity_id'],)).fetchone():
        raise MigrationError(f'Listening subject has no captured account: {SOURCES[kind]} {source["id"]}')
    value = dict(source, account_id=source['identity_id'],
                 unresolved_subject_key=source['identity_key'] if recording is None else None)
    if kind == 'events':
        # Old rows did not capture a retained membership ID. Today's membership
        # with the same provider identifier does not establish that old context.
        value['membership_ref_id'] = None
    if value.get('capture_id') is not None and not conn.execute(
            'SELECT 1 FROM main.listening_capture_receipts WHERE id=? AND identity_id=?',
            (value['capture_id'], value['account_id'])).fetchone():
        raise MigrationError(f'Listening observation has no matching capture: {source["id"]}')
    result = {name:value.get(name) for name in FIELDS[kind]}
    result['source_snapshot'] = json.dumps(source, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(result['source_snapshot'].encode()) > MAX_ROW_BYTES:
        raise MigrationError('Listening source exceeds its byte budget')
    return result


def _copy(conn, kind, row):
    value = describe(conn, kind, row)
    fields = tuple(value)
    updates = ','.join(name + '=excluded.' + name for name in fields if name != 'id')
    conn.execute(f'INSERT INTO {DESTINATIONS[kind]}(' + ','.join(fields) + ') VALUES (' +
                 ','.join('?' for _ in fields) + ') ON CONFLICT(id) DO UPDATE SET ' + updates, tuple(value.values()))


def step(conn, *, batch_size=128, retry=False, clock=time.time):
    """Commit at most one bounded batch; failure requires an explicit retry.

    Staging intentionally has no FKs into mutable serving owners. A stale staged
    row must not block an account/source reset or require synchronous cascading
    work. The journal, local subject checks and preparation revision establish
    the prospective copy; future owner publication needs its own checked fence.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 512:
        raise ValueError('Listening preparation batch size must be from 1 through 512')
    # The supplied number is an upper bound. Keep even historical callers with
    # a larger allowance within the current transaction's row and byte budget.
    batch_size = min(batch_size, MAX_BATCH_ROWS)
    if conn.in_transaction:
        raise MigrationError('Cannot prepare listening with an outstanding caller transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        _version(conn)
        _validate_schema(conn)
        current = progress(conn)
        if current is None:
            raise MigrationError('Listening preparation is not initialized')
        if current.status == 'failed' and not retry:
            raise MigrationError('Listening preparation failed; repair the reported input and retry explicitly: ' + current.error)
        if current.status == 'prepared':
            conn.rollback()
            return current
        conn.execute('SAVEPOINT listening_batch')
        try:
            processed = 0
            phase, cursor = current.phase, current.cursor
            if phase in SOURCES:
                highwater = conn.execute(f'SELECT {phase}_highwater FROM {STATE} WHERE id=1').fetchone()[0]
                rows = bounded_rows(conn, f'SELECT * FROM main.{SOURCES[phase]} WHERE id<=?' +
                    (' AND id>?' if cursor is not None else '') + ' ORDER BY id LIMIT ?',
                    (highwater, cursor, batch_size) if cursor is not None else (highwater, batch_size))
                for row in rows:
                    _copy(conn, phase, row)
                    conn.execute(f'DELETE FROM {CHANGES} WHERE kind=? AND source_id=?', (phase, row['id']))
                processed = len(rows)
                cursor = rows[-1]['id'] if rows else cursor
                if len(rows) < batch_size:
                    phase, cursor = ('observations' if phase == 'events' else 'changes'), None
            else:
                changes = conn.execute(f'SELECT kind,source_id FROM {CHANGES} ORDER BY kind,source_id LIMIT ?',
                                       (batch_size,)).fetchall()
                for kind, source_id in changes:
                    source_rows = bounded_rows(conn, f'SELECT * FROM main.{SOURCES[kind]} WHERE id=?', (source_id,))
                    row = source_rows[0] if source_rows else None
                    if row is None:
                        conn.execute(f'DELETE FROM {DESTINATIONS[kind]} WHERE id=?', (source_id,))
                    else:
                        _copy(conn, kind, row)
                    conn.execute(f'DELETE FROM {CHANGES} WHERE kind=? AND source_id=?', (kind, source_id))
                processed = len(changes)
            empty = phase == 'changes' and not conn.execute(f'SELECT 1 FROM {CHANGES} LIMIT 1').fetchone()
            status = 'prepared' if empty else 'reconciling' if phase == 'changes' else 'copying'
            conn.execute(f'UPDATE {STATE} SET phase=?,cursor=?,status=?,error=NULL,processed=processed+?,updated_at=?,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END WHERE id=1',
                         (phase, cursor, status, processed, clock(), int(empty)))
        except Exception as exc:
            # Preserve earlier committed batches and retain a useful durable
            # failure without repeatedly scheduling the same broken conversion.
            if not conn.in_transaction:
                raise
            conn.execute('ROLLBACK TO listening_batch')
            conn.execute(f"UPDATE {STATE} SET status='failed',prepared_revision=NULL,error=?,updated_at=? WHERE id=1",
                         (str(exc)[:2000], clock()))
            conn.commit()
            raise
        conn.execute('RELEASE listening_batch')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return progress(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    from pathlib import Path

    def bounded_number(value):
        number = int(value)
        if not 1 <= number <= 512:
            raise argparse.ArgumentTypeError('must be from 1 through 512')
        return number

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--batches', type=bounded_number, default=1,
                        help='maximum committed batches this invocation (default: 1)')
    parser.add_argument('--batch-size', type=bounded_number, default=128)
    parser.add_argument('--retry', action='store_true', help='retry a previously reported failure explicitly')
    parser.add_argument('--status', action='store_true', help='inspect existing progress read-only')
    args = parser.parse_args(argv)
    mode = 'ro' if args.status else 'rw'
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri() + '?mode=' + mode,
                                     uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.status:
                present = conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (STATE,)).fetchone()
                result = progress(conn) if present else None
            else:
                result = install(conn)
                for _ in range(args.batches):
                    result = step(conn, batch_size=args.batch_size, retry=args.retry)
                    if result.status == 'prepared':
                        break
            print(json.dumps(asdict(result) if result is not None else {'status': 'uninitialized'}, sort_keys=True))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'Listening preparation failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
