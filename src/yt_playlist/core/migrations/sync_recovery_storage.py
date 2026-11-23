"""Keep sync and collection administration on their existing storage owners."""
from .. import collection_recovery_epoch
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from ..sync_state import IMPORT_STAMPS
from .account_storage import _preserve_sequence, _sequence
from .maintenance_clocks import IMPORT_CHECKPOINT, PLAYBACK_CLOCK


NAME = 'consolidate_sync_and_collection_recovery'
CURSORS = 'sync_cursors'
RECEIPTS = 'collection_file_writes'
RETIRED = ('sync_attempts', 'collection_recovery_epoch')
OWNERS = (CURSORS, RECEIPTS, *RETIRED)

CURSOR_COLUMNS = ('account_id', 'provider', 'collection', 'playlist_scope_id', 'cursor', 'phase',
                  'revision', 'updated_at', 'last_success_at', 'failures', 'retry_at')
ATTEMPT_COLUMNS = ('revision', 'phase', 'outcome', 'started_at', 'finished_at', 'last_success_at',
                   'recovery_filename', 'recovery_digest', 'recovery_bytes', 'last_plays_sync_at',
                   'verified_imports_repaired', *IMPORT_STAMPS)
RECEIPT_COLUMNS = 'id,kind,owner_id,content_digest,byte_size,file_status'


def _time(column):
    return (f'{column} REAL CHECK({column} IS NULL OR '
            f"(typeof({column}) IN ('integer','real') AND abs({column})<=1.7976931348623157e308))")


CURSOR_SQL = f'''CREATE TABLE sync_cursors (
 id INTEGER PRIMARY KEY,
 account_id INTEGER REFERENCES accounts(id),
 provider TEXT NOT NULL DEFAULT 'youtube' CHECK(provider='youtube'),
 collection TEXT NOT NULL CHECK(collection IN ('attempt','account','playlist_listing','playlists','ratings',
    'history','saved_albums','saved_songs','saved_album_mirror','album_tracks','playlist')),
 playlist_scope_id TEXT REFERENCES playlist_scopes(id),
 cursor TEXT,
 phase TEXT NOT NULL CHECK(phase IN ('running','complete','partial','failed','skipped')),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
 {_time('updated_at')},
 {_time('last_success_at')},
 failures INTEGER CHECK(failures IS NULL OR (typeof(failures)='integer' AND failures BETWEEN 0 AND 16)),
 {_time('retry_at')},
 outcome TEXT CHECK(outcome IN ('complete','partial','failed','skipped')),
 {_time('started_at')},
 {_time('finished_at')},
 recovery_filename TEXT,
 recovery_digest TEXT CHECK(recovery_digest IS NULL OR length(recovery_digest)=64),
 recovery_bytes INTEGER CHECK(recovery_bytes IS NULL OR
    (typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)),
 {PLAYBACK_CLOCK},
 {IMPORT_CHECKPOINT},
 {', '.join(column + ' TEXT' for column in IMPORT_STAMPS)},
 recovery_owner INTEGER GENERATED ALWAYS AS (CASE WHEN id=0 THEN 1 END) VIRTUAL,
 CHECK((collection='playlist') = (playlist_scope_id IS NOT NULL)),
 CHECK(account_id IS NOT NULL OR collection IN ('attempt','saved_album_mirror','album_tracks')),
 CHECK((recovery_filename IS NULL AND recovery_digest IS NULL AND recovery_bytes IS NULL) OR
       (recovery_filename IS NOT NULL AND recovery_digest IS NOT NULL AND recovery_bytes IS NOT NULL)),
 CHECK((id=0 AND collection='attempt' AND account_id IS NULL AND cursor IS NULL AND updated_at IS NULL
       AND failures IS NULL AND retry_at IS NULL) OR
       (id>0 AND collection<>'attempt' AND phase<>'running' AND outcome IS NULL AND started_at IS NULL
       AND finished_at IS NULL AND recovery_filename IS NULL AND last_plays_sync_at IS NULL
       AND verified_imports_repaired IS NULL AND {' AND '.join(column + ' IS NULL' for column in IMPORT_STAMPS)}))
)'''

RECEIPT_SQL = '''CREATE TABLE collection_file_writes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT,
 owner_id TEXT,
 content_digest TEXT CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
 byte_size INTEGER CHECK(byte_size IS NULL OR (typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 16777216)),
 file_status TEXT CHECK(file_status IN ('pending','ready')),
 ownership_revision INTEGER,
 CHECK((id=0 AND typeof(ownership_revision)='integer' AND ownership_revision>=0 AND kind IS NULL
       AND owner_id IS NULL AND content_digest IS NULL AND byte_size IS NULL AND file_status IS NULL) OR
       (id>0 AND ownership_revision IS NULL AND kind IS NOT NULL AND owner_id IS NOT NULL
       AND content_digest IS NOT NULL AND byte_size IS NOT NULL AND file_status IS NOT NULL))
)'''


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "(tbl_name IN (?,?,?,?) OR name GLOB 'collection_recovery_changed_*') ORDER BY type,name", OWNERS)]


def folded(conn):
    return 'ownership_revision' in tokens(definitions(conn, (RECEIPTS,)).get(RECEIPTS))


def _rebuild(conn, table, create, columns):
    previous = 'previous_' + table
    conn.execute(f'ALTER TABLE {table} RENAME TO {previous}')
    conn.execute(create)
    conn.execute(f'INSERT INTO {table}({columns}) SELECT {columns} FROM {previous}')
    conn.execute(f'DROP TABLE {previous}')


def install(conn, _context):
    cursor_objects = [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name='sync_cursors' AND type IN ('index','trigger') "
        "AND sql IS NOT NULL ORDER BY type,name")]
    for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' "
                              "AND name GLOB 'collection_recovery_changed_*'").fetchall():
        conn.execute(f'DROP TRIGGER {name}')
    _rebuild(conn, CURSORS, CURSOR_SQL, ','.join(('id', *CURSOR_COLUMNS)))
    attempt = ','.join(ATTEMPT_COLUMNS)
    conn.execute(f"INSERT INTO {CURSORS}(id,collection,{attempt}) SELECT 0,'attempt',{attempt} FROM sync_attempts")
    conn.execute('DROP TABLE sync_attempts')
    for sql in cursor_objects:
        conn.execute(sql)

    sequence = _sequence(conn, RECEIPTS)
    _rebuild(conn, RECEIPTS, RECEIPT_SQL, RECEIPT_COLUMNS)
    _preserve_sequence(conn, RECEIPTS, sequence)
    conn.execute(f'INSERT INTO {RECEIPTS}(id,ownership_revision) '
                 'SELECT 0,revision FROM collection_recovery_epoch WHERE id=1')
    conn.execute('DROP TABLE collection_recovery_epoch')
    collection_recovery_epoch.install(conn)
    collection_recovery_epoch.install_keys(conn)


def validate(conn):
    from . import playlist_bookkeeping, shared_file_receipts

    installed = definitions(conn, OWNERS)
    backup_receipts = definitions(conn, (playlist_bookkeeping.MARKER,))
    receipts = playlist_bookkeeping.backup_receipt_table(RECEIPT_SQL) if backup_receipts else RECEIPT_SQL
    if definitions(conn, (shared_file_receipts.MARKER,)):
        checked_slots = '\n CHECK((id=0'
        receipts = receipts.replace(checked_slots, ' ' + ', '.join(shared_file_receipts.STAGED_COLUMNS) + ',' + checked_slots, 1)
    if (set(installed) != {CURSORS, RECEIPTS} or tokens(installed[CURSORS]) != tokens(CURSOR_SQL)
            or tokens(installed[RECEIPTS]) != tokens(receipts)):
        raise MigrationError('Incomplete sync and collection recovery consolidation')
    collection_recovery_epoch.revision(conn)
