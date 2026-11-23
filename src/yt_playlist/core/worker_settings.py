"""Read and update named worker settings inside the caller's transaction."""
from .db_revisions import setting_reads


def number(conn, key):
    return int(read(conn, key))


def numbers(conn, *keys):
    marks = ','.join('?' for _ in keys)
    with setting_reads(conn, keys):
        values = dict(conn.execute(f'SELECT key,value FROM settings WHERE key IN ({marks})', keys))
    return tuple(int(values[key]) for key in keys)


def read(conn, key, default=None):
    with setting_reads(conn, (key,)):
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row is not None else default


def save(conn, key, value):
    if value is None:
        conn.execute('DELETE FROM settings WHERE key=?', (key,))
    else:
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value',
                     (key, value))


def increment_sql(key):
    return f"UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='{key}'"
