"""Keep playlist sync revisions on scopes and backup receipts with collection files."""
import re

from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .track_source_cutover import _replace_table


NAME = 'own_playlist_bookkeeping'
SCOPES = 'playlist_scopes'
RECEIPTS = 'collection_file_writes'
RETIRED = ('playlist_library_revisions', 'playlist_backup_writes')
MARKER = 'ix_playlist_backup_capture'
BACKUP_FIELDS = ('playlist_id', 'playlist_account_id', 'playlist_provider_id', 'playlist_captured_at')
BACKUP_KIND = 'playlist-backup'
OWNERS = (SCOPES, RECEIPTS, *RETIRED)

_INCREMENT = re.compile(r'INSERT INTO playlist_library_revisions SELECT (.+?),(.+?),1(.*?) '
                        r'ON CONFLICT\(playlist_scope_id,account_id\) DO UPDATE SET revision=revision\+1;', re.S)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        "(tbl_name IN (?,?,?,?) OR name GLOB 'playlist_revision_*') ORDER BY type,name", OWNERS)]


def _increment(match):
    scope, account, where = match.groups()
    changed = f'SELECT {scope} AS playlist_scope_id,{account} AS account_id{where}'
    return f'''UPDATE playlist_scopes SET library_revisions=json_patch(library_revisions,(
        SELECT json_group_object(a.account_id,COALESCE(json_extract(library_revisions,'$."'||a.account_id||'"'),0)+a.delta)
        FROM (SELECT account_id,count(*) AS delta FROM ({changed})
              WHERE playlist_scope_id=playlist_scopes.id GROUP BY account_id) a))
        WHERE id IN (SELECT playlist_scope_id FROM ({changed}));'''


def _install_revisions(conn):
    conn.execute("ALTER TABLE playlist_scopes ADD COLUMN library_revisions TEXT NOT NULL DEFAULT '{}' "
                 "CHECK(json_type(library_revisions)='object')")
    conn.execute('''UPDATE playlist_scopes SET library_revisions=(
        SELECT json_group_object(account_id,revision ORDER BY account_id) FROM playlist_library_revisions
        WHERE playlist_scope_id=playlist_scopes.id)
        WHERE id IN (SELECT playlist_scope_id FROM playlist_library_revisions)''')
    guards = conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name GLOB 'playlist_revision_*'")
    for name, sql in guards.fetchall():
        changed = _INCREMENT.sub(_increment, sql)
        if changed == sql or RETIRED[0] in changed:
            raise MigrationError('Unable to move playlist revision trigger ' + name)
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(changed)
    conn.execute('DROP TABLE playlist_library_revisions')

    for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF library_revisions')):
        conn.execute(f'''CREATE TRIGGER owned_playlist_revisions_{suffix} BEFORE {event} ON playlist_scopes
            WHEN EXISTS(SELECT 1 FROM json_each(NEW.library_revisions) r
                LEFT JOIN accounts a ON a.id=CAST(r.key AS INTEGER)
                WHERE r.type<>'integer' OR r.value<0 OR CAST(a.id AS TEXT) IS NOT r.key
                   OR (NEW.kind='account' AND NEW.account_id IS NOT a.id))
              OR EXISTS(SELECT 1 FROM json_each(NEW.library_revisions) GROUP BY key HAVING count(*)>1)
            BEGIN SELECT RAISE(ABORT,'Invalid playlist scope revisions'); END''')
    conn.execute('''CREATE TRIGGER retained_playlist_scope_revisions BEFORE DELETE ON playlist_scopes
        WHEN EXISTS(SELECT 1 FROM json_each(OLD.library_revisions))
        BEGIN SELECT RAISE(ABORT,'Playlist scope still owns sync revisions'); END''')
    for suffix, event in (('delete', 'DELETE'), ('update', 'UPDATE OF id')):
        changed = 'NEW.id IS NOT OLD.id AND ' if suffix == 'update' else ''
        conn.execute(f'''CREATE TRIGGER retained_playlist_revision_account_{suffix} BEFORE {event} ON accounts
            WHEN {changed}EXISTS(SELECT 1 FROM playlist_scopes
                WHERE json_extract(library_revisions,'$."'||OLD.id||'"') IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'Account still owns playlist sync revisions'); END''')


def backup_receipt_table(table):
    existing_check = '\n CHECK((id=0'
    if 'BETWEEN 1 AND 16777216' not in table or existing_check not in table:
        raise MigrationError('Unexpected collection file receipt layout')
    table = table.replace('BETWEEN 1 AND 16777216',
                          f"BETWEEN 1 AND CASE WHEN kind='{BACKUP_KIND}' THEN 67108864 ELSE 16777216 END")
    table = table.replace(existing_check, '''
        playlist_id INTEGER,
        playlist_account_id INTEGER,
        playlist_provider_id TEXT,
        playlist_captured_at REAL,''' + existing_check, 1)
    return table.rstrip().removesuffix(')') + f''',
        CHECK((kind IS '{BACKUP_KIND}' AND length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'
                AND playlist_account_id IS NOT NULL AND playlist_provider_id IS NOT NULL
                AND length(playlist_provider_id)>0 AND playlist_captured_at IS NOT NULL)
            OR (kind IS NOT '{BACKUP_KIND}' AND {' AND '.join(field + ' IS NULL' for field in BACKUP_FIELDS)}))
    )'''


def _install_backup_receipts(conn):
    table = backup_receipt_table(definitions(conn, (RECEIPTS,))[RECEIPTS])
    guards = [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? '
                                          "AND type IN ('trigger','index') AND sql IS NOT NULL", (RECEIPTS,))]
    sequence = _sequence(conn, RECEIPTS)
    _replace_table(conn, RECEIPTS, table, expressions={field: 'NULL' for field in BACKUP_FIELDS})
    _preserve_sequence(conn, RECEIPTS, sequence)
    conn.execute(f'INSERT INTO {RECEIPTS}(kind,owner_id,{",".join(BACKUP_FIELDS)},content_digest,byte_size,file_status) '
        'SELECT ?,id,playlist_id,account_id,provider_playlist_id,captured_at,content_digest,byte_size,file_status '
        'FROM playlist_backup_writes ORDER BY id', (BACKUP_KIND,))
    conn.execute('DROP TABLE playlist_backup_writes')
    for sql in guards:
        conn.execute(sql)
    conn.execute(f"CREATE UNIQUE INDEX {MARKER} ON {RECEIPTS}(owner_id) WHERE kind='{BACKUP_KIND}'")


def install(conn, _context):
    _install_revisions(conn)
    _install_backup_receipts(conn)


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Incompatible playlist bookkeeping storage')
