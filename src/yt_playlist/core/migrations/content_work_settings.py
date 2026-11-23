"""Preserve ordered, outstanding content refreshes through the settings cutover."""
from .. import content_work, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _sequence

NAME = 'keep_content_work_in_settings'
RETIRED = 'rec_content_pending'


def _statements(conn):
    return conn.execute("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL "
                        "AND (tbl_name=? OR (type='trigger' AND instr(sql,?))) ORDER BY type,name",
                        (RETIRED, RETIRED)).fetchall()


def predecessor_objects(conn):
    return [sql for _, sql in _statements(conn)]


def install(conn, _context):
    from ..content_work import INDEX, PENDING, POSITION, PREFIX, SEQUENCE

    worker_settings.save(conn, SEQUENCE, str(_sequence(conn, RETIRED) or 0))
    conn.execute(f"INSERT INTO settings(key,value) SELECT ?||identity_key,json_array(position,revision) FROM {RETIRED}",
                 (PREFIX,))
    for name, sql in _statements(conn):
        if name == RETIRED:
            continue
        updated = content_work.upgrade_enqueue_sql(sql)
        if RETIRED in updated:
            raise MigrationError(f'Unknown content invalidation statement in {name}')
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(updated)
    conn.execute('DROP TABLE ' + RETIRED)
    conn.execute(f'CREATE INDEX {INDEX} ON settings(({POSITION})) WHERE {PENDING}')
    advance = worker_settings.increment_sql(SEQUENCE)
    conn.execute(f'''CREATE TRIGGER content_pending_assign AFTER INSERT ON settings WHEN NEW.{PENDING}
        BEGIN {advance};
            UPDATE settings SET value=json_set(value,'$[0]',
                (SELECT CAST(value AS INTEGER) FROM settings WHERE key='{SEQUENCE}')) WHERE key=NEW.key;
        END''')
    conn.execute(f'''CREATE TRIGGER content_pending_changed AFTER UPDATE OF value ON settings
        WHEN NEW.{PENDING} AND OLD.value->>1 IS NOT NEW.value->>1 BEGIN {advance}; END''')
    conn.execute(f'''CREATE TRIGGER content_pending_removed AFTER DELETE ON settings WHEN OLD.{PENDING}
        BEGIN {advance}; END''')


def validate(conn):
    names = (content_work.INDEX, 'content_pending_assign', 'content_pending_changed', 'content_pending_removed')
    if set(definitions(conn, (*names, RETIRED))) != set(names):
        raise MigrationError('Incomplete content work settings cutover')
