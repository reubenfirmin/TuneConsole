"""Retention progress and active listening windows on the shared receipt owner."""
import json


COLUMN = 'evidence_maintenance'
DEFAULT = {'pending': 0, 'phase': 'inventory', 'checkpoint_after': ''}


def state(conn):
    row = conn.execute(f'SELECT {COLUMN} FROM collection_file_writes WHERE id=0').fetchone()
    saved = json.loads(row[0]) if row[0] is not None else {}
    return {key: saved.get(key, default) for key, default in DEFAULT.items()}


def update(conn, changes):
    conn.execute(f"UPDATE collection_file_writes SET {COLUMN}=json_patch(coalesce({COLUMN},'{{}}'),?) WHERE id=0",
                 (json.dumps(changes),))


def window(conn, format):
    row = conn.execute(f'SELECT json_extract({COLUMN},?) FROM collection_file_writes WHERE id=0',
                       (f'$.windows.{format}',)).fetchone()
    if row[0] is None:
        return None
    value = json.loads(row[0])
    value.pop('updated_at')
    return value


def write_window(conn, format, value, at):
    path = f'$.windows.{format}'
    if value:
        conn.execute(f"UPDATE collection_file_writes SET {COLUMN}=json_set(coalesce({COLUMN},'{{}}'),?,json(?)) "
                     f"WHERE id=0 AND coalesce(json_extract({COLUMN},?)<=?,1)",
                     (path, json.dumps({**value, 'updated_at': at}), path + '.updated_at', at))
    else:
        conn.execute(f'UPDATE collection_file_writes SET {COLUMN}=json_remove({COLUMN},?) WHERE id=0', (path,))
