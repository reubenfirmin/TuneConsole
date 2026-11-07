"""Finite control selections without preparation-worker dependencies."""
import json


NATIVE = {'album-controls': 'album_menu_controls', 'model-pointers': 'model_selection_heads'}


def installed(conn, name):
    return (conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_control_heads'").fetchone() is not None
            and conn.execute('SELECT 1 FROM published_control_heads WHERE name=?', (name,)).fetchone() is not None)


def selected(conn, name):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (NATIVE[name],)).fetchone():
        return None
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_control_heads'").fetchone():
        return None
    row = conn.execute('SELECT revision FROM published_control_heads WHERE name=? AND active=1', (name,)).fetchone()
    return row[0] if row else None


def album_read(conn, revision, default):
    row = conn.execute('SELECT expires_at,novelty,source_kind FROM published_album_controls WHERE revision=?', (revision,)).fetchone()
    if row is None or row['source_kind'] != 'controls':
        return default
    genres = [row[0] for row in conn.execute('SELECT coalesce(genre_key,raw_label) FROM published_album_control_genres '
                                           'WHERE revision=? ORDER BY position', (revision,))]
    return json.dumps({'until': row['expires_at'], 'values': {'novelty': row['novelty']}, 'genres': genres})


def pointer_read(conn, revision, key, default):
    row = conn.execute('SELECT recorded_run_id FROM published_model_selection_pointers WHERE revision=? AND source=?',
                       (revision, key)).fetchone()
    return (str(row[0]) if row[0] is not None else '') if row else default


def retained_write(conn, values):
    for key, value in values.items():
        if value is None:
            conn.execute('DELETE FROM main.settings WHERE key=?', (key,))
        else:
            conn.execute('INSERT INTO main.settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (key, value))
