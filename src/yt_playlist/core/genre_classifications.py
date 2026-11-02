"""Current single genre selections, with explicit unresolved source labels."""
from . import genre_identity
from .genre_contract import OWNERS, available, expression, published_expression


def read_expression(conn, alias, table):
    if available(conn):
        return expression(alias)
    return published_expression(conn, alias, table)


def read(conn, table, subject):
    column = OWNERS[table]
    selected = read_expression(conn, 's', table)
    row = conn.execute(f'SELECT {selected} FROM {table} s WHERE {column}=?', (subject,)).fetchone()
    return row[0] if row is not None else None


def write(conn, table, subject, label):
    column = OWNERS[table]
    if not available(conn):
        conn.execute(f'UPDATE {table} SET genre=? WHERE {column}=? AND genre IS NOT ?',
                     (label, subject, label))
        return
    key, reason = genre_identity.resolve(conn, label)
    unresolved = label if key is None else None
    _write(conn, table, subject, key, unresolved)


def _write(conn, table, subject, key, unresolved):
    column = OWNERS[table]
    conn.execute(f'UPDATE {table} SET genre_key=?,unresolved_genre=? WHERE {column}=? '
                 'AND (genre_key IS NOT ? OR unresolved_genre IS NOT ?)',
                 (key, unresolved, subject, key, unresolved))


def write_evidence(conn, table, subject, value):
    """Project the captured identity, including a deliberately unresolved label.

    Replaying evidence is not a new alias lookup. Only an explicit genre merge
    follows a captured key to its current identity.
    """
    if not available(conn):
        write(conn, table, subject, value.selected())
        return
    key = genre_identity.current_key(conn,value.genre_key) if value.genre_key is not None else None
    _write(conn,table,subject,key,value.selected() if key is None else None)


def copy_recording(conn, previous, target):
    """Transfer an unattributed selection without inventing new alias evidence."""
    if not available(conn):
        write(conn,'recordings',target,read(conn,'recordings',previous))
        return
    key, raw = conn.execute('SELECT genre_key,unresolved_genre FROM recordings WHERE id=?',(previous,)).fetchone()
    _write(conn,'recordings',target,genre_identity.current_key(conn,key) if key is not None else None,raw)


def selected_label(conn, value):
    """Render selected evidence without modifying its immutable captured label."""
    if value.genre_key is not None:
        row = conn.execute('SELECT name FROM genre_catalog WHERE key=?',
                            (genre_identity.current_key(conn,value.genre_key),)).fetchone()
        if row is not None:
            return row[0]
    return value.selected()
