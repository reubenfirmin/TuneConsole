"""Keep grouping progress, input serial and retention cursor on one row."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'fold_group_checkpoint_storage'
TABLE = 'group_preparation_state'
RETIRED = ('group_preparation_progress', 'rec_group_clock', 'group_retention_state',
           'group_retention_epoch', 'group_retention_walk')
COLUMNS = {
    'serial': 'INTEGER NOT NULL DEFAULT 0',
    'roots_complete': "INTEGER NOT NULL DEFAULT 0 CHECK(typeof(roots_complete)='integer' AND roots_complete IN (0,1))",
    'retention_epoch': 'INTEGER NOT NULL DEFAULT 0',
    'walk_revision': 'INTEGER',
    'walk_phase': 'TEXT',
    'walk_source': 'INTEGER NOT NULL DEFAULT 0',
    'walk_cursor': "TEXT NOT NULL DEFAULT ''",
    'walk_cursor2': "TEXT NOT NULL DEFAULT ''",
    'walk_cursor3': "TEXT NOT NULL DEFAULT ''",
}
STATEMENT_REPLACEMENTS = {
    'UPDATE rec_group_clock SET serial=serial+1 WHERE id=1':
        'UPDATE group_preparation_state SET serial=serial+1 WHERE singleton=1',
    'UPDATE group_retention_epoch SET revision=revision+1 WHERE id=1':
        'UPDATE group_preparation_state SET retention_epoch=retention_epoch+1 WHERE singleton=1',
    'FROM group_retention_state WHERE id=1 AND roots_complete=1':
        'FROM group_preparation_state WHERE singleton=1 AND roots_complete=1',
}


def _triggers(conn):
    return {name: sql for name, owner, sql in conn.execute(
        "SELECT name,tbl_name,sql FROM sqlite_schema WHERE type='trigger' AND sql IS NOT NULL ORDER BY name")
            if owner == TABLE or any(fragment in sql for fragment in STATEMENT_REPLACEMENTS)}


def predecessor_objects(conn):
    previous = definitions(conn, (TABLE, *RETIRED))
    return [previous[name] for name in (TABLE, *RETIRED)] + list(_triggers(conn).values())


def install(conn, _context):
    triggers = _triggers(conn)
    for name in triggers:
        conn.execute('DROP TRIGGER ' + name)
    sql = definitions(conn, (TABLE,))[TABLE]
    sql = sql.replace("'preparing','ready','deferred'", "'preparing','ready','deferred','failed'")
    columns = ',\n '.join(f'{column} {declaration}' for column, declaration in COLUMNS.items())
    sql = sql.replace(' CHECK((phase IS NULL', ' ' + columns + ',\n CHECK((phase IS NULL', 1)
    _replace_table(conn, TABLE, sql,
                   expressions={column: (
                       '0' if column in ('serial', 'roots_complete', 'retention_epoch', 'walk_source')
                       else "''" if column.startswith('walk_cursor') else 'NULL') for column in COLUMNS})
    conn.execute(f'INSERT OR IGNORE INTO {TABLE}(singleton) VALUES (1)')
    conn.execute(f'''
        UPDATE {TABLE} SET
            serial=(SELECT serial FROM rec_group_clock WHERE id=1),
            roots_complete=(SELECT roots_complete FROM group_retention_state WHERE id=1),
            retention_epoch=(SELECT revision FROM group_retention_epoch WHERE id=1),
            (phase,reported_at,reason)=(
                 SELECT phase,reported_at,reason FROM group_preparation_progress WHERE singleton=1
            ),
            walk_revision=(SELECT revision FROM group_retention_walk WHERE id=1),
            walk_phase=(SELECT phase FROM group_retention_walk WHERE id=1),
            (walk_source,walk_cursor,walk_cursor2,walk_cursor3)=(SELECT
                coalesce(w.source,0),coalesce(w.cursor,''),coalesce(w.cursor2,''),coalesce(w.cursor3,'')
                FROM (SELECT 1 id) seed LEFT JOIN group_retention_walk w USING(id))
        WHERE singleton=1
    ''')
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    for name, sql in triggers.items():
        if name.startswith('group_progress_legacy_'):
            continue
        for previous, current in STATEMENT_REPLACEMENTS.items():
            sql = sql.replace(previous, current)
        conn.execute(sql)


def validate(conn):
    if definitions(conn, RETIRED) or conn.execute(
            f'SELECT 1 FROM {TABLE} WHERE singleton=1 AND serial>=0 AND retention_epoch>=0').fetchone() is None:
        raise MigrationError('Grouping checkpoint storage was not folded')
