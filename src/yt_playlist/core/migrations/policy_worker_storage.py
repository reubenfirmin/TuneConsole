"""Own policy archive references and IDs in the existing history table."""
from .. import policy_history_read
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .policy_archive_storage import HISTORY
from .track_source_cutover import _replace_table


NAME = 'own_policy_retirement_state'


def predecessor_objects(conn):
    return list(definitions(conn, (HISTORY,)).values())


def install(conn, _context):
    layout = definitions(conn, (HISTORY,))[HISTORY]
    layout = layout.removesuffix(' WITHOUT ROWID').replace(
        'version TEXT PRIMARY KEY NOT NULL', 'id INTEGER PRIMARY KEY AUTOINCREMENT,version TEXT NOT NULL UNIQUE', 1)
    layout = layout[:-1] + ", shared_archive_head TEXT CHECK(shared_archive_head IS NULL OR " \
        "(state='archived' AND archive_file_id IS NULL AND length(shared_archive_head)=64 " \
        "AND shared_archive_head NOT GLOB '*[^0-9a-f]*')))"
    attached = [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? '
        "AND type<>'table' AND sql IS NOT NULL ORDER BY type,name", (HISTORY,))]
    _replace_table(conn, HISTORY, layout, expressions={'id': 'NULL', 'shared_archive_head':
        "CASE WHEN state='archived' AND archive_file_id IS NULL THEN "
        f"(SELECT value FROM settings WHERE key='{policy_history_read.ARCHIVE_HEAD}') END"})
    for sql in attached:
        conn.execute(sql)
    conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (HISTORY,))
    conn.execute(f'''INSERT INTO sqlite_sequence(name,seq)
        SELECT ?,max(coalesce(max(id),0),coalesce(max(archive_file_id),0),
            coalesce((SELECT CAST(value AS INTEGER) FROM settings WHERE key=?),0)) FROM {HISTORY}''',
        (HISTORY, policy_history_read.NEXT_ARCHIVE_FILE))
    conn.execute('DELETE FROM settings WHERE key IN (?,?,?,?)', (policy_history_read.ARCHIVE_HEAD,
        policy_history_read.NEXT_ARCHIVE_FILE, 'rec_policy_retention_cursor', 'rec_unused_policy_cursor'))


def validate(conn):
    layout = tokens(definitions(conn, (HISTORY,)).get(HISTORY))
    if 'shared_archive_head' not in layout or 'autoincrement' not in layout:
        raise MigrationError('Invalid policy history storage')
