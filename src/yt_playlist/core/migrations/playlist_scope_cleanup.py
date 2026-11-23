"""Consolidate playlist cleanup preferences on their existing owners."""
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'consolidate_playlist_scope_cleanup'
DECISIONS = 'playlist_overlap_decisions'
RETIRED = ('cleanup_ignored', 'ignored_merge_members', 'overlap_kept', 'suppressed_overlaps')


def install(conn, _context):
    conn.execute("ALTER TABLE playlist_scopes ADD COLUMN cleanup_dismissals TEXT NOT NULL DEFAULT '{}' "
                 "CHECK(json_type(cleanup_dismissals)='object')")
    # SQLite's default JSON number rendering rounds REAL timestamps.
    conn.execute('''UPDATE playlist_scopes SET cleanup_dismissals=dismissed.categories
        FROM (SELECT playlist_scope_id,json_group_object(category,
                     CASE WHEN created_at IS NOT NULL THEN json(printf('%!.26g',created_at)) END) AS categories
              FROM cleanup_ignored GROUP BY playlist_scope_id) AS dismissed
        WHERE id=dismissed.playlist_scope_id''')
    conn.execute(f'ALTER TABLE suppressed_overlaps RENAME TO {DECISIONS}')
    conn.execute(f'ALTER TABLE {DECISIONS} RENAME COLUMN created_at TO suppressed_at')
    conn.execute(f'ALTER TABLE {DECISIONS} ADD COLUMN is_kept INTEGER NOT NULL DEFAULT 0 '
                 'CHECK(is_kept IN (0,1))')
    conn.execute(f'ALTER TABLE {DECISIONS} ADD COLUMN kept_at REAL CHECK(is_kept=1 OR kept_at IS NULL)')
    conn.execute(f'ALTER TABLE {DECISIONS} ADD COLUMN is_suppressed INTEGER NOT NULL DEFAULT 1 '
                 'CHECK(is_suppressed IN (0,1) AND (is_kept=1 OR is_suppressed=1) '
                 'AND (is_suppressed=1 OR suppressed_at IS NULL))')
    conn.execute(f'''INSERT INTO {DECISIONS}
        (first_playlist_scope_id,second_playlist_scope_id,is_suppressed,is_kept,kept_at)
        SELECT first_playlist_scope_id,second_playlist_scope_id,0,1,created_at FROM overlap_kept
        WHERE true ON CONFLICT(first_playlist_scope_id,second_playlist_scope_id) DO UPDATE
        SET is_kept=1,kept_at=excluded.kept_at''')
    for table in RETIRED[:-1]:
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    actual = objects(conn, (DECISIONS, *RETIRED))
    if actual.get(DECISIONS, ('',))[0] != 'table' or actual.keys() & set(RETIRED):
        raise MigrationError('Incompatible playlist overlap decisions')
