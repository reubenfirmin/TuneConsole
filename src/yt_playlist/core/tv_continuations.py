"""Completed TV requests on the playlist membership they continued."""
import json

from . import worker_settings


COLUMN = 'tv_continuations'
DECLARATION = f"{COLUMN} TEXT CHECK({COLUMN} IS NULL OR json_type({COLUMN}) IS 'array')"
UNRESOLVED = 'tv_unresolved_continuations'
ROWS = """SELECT m.id AS membership, j.value AS document
        FROM playlists m JOIN json_each(m.tv_continuations) j
        WHERE m.tv_continuations IS NOT NULL
        UNION ALL SELECT NULL, j.value FROM settings s JOIN json_each(s.value) j WHERE s.key=?"""


def _membership(conn, source):
    row = conn.execute('SELECT id FROM playlists WHERE id=?', (source,)).fetchone()
    return row[0] if row else None


def entries(conn, membership):
    if membership is None:
        raw = worker_settings.read(conn, UNRESOLVED)
    else:
        row = conn.execute(f'SELECT {COLUMN} FROM playlists WHERE id=?', (membership,)).fetchone()
        raw = row[0] if row else None
    return json.loads(raw) if raw is not None else []


def save(conn, membership, rows):
    encoded = json.dumps(rows) if rows else None
    if membership is None:
        worker_settings.save(conn, UNRESOLVED, encoded)
    else:
        conn.execute(f'UPDATE playlists SET {COLUMN}=? WHERE id=?', (encoded, membership))


def header(conn, key, source):
    membership = _membership(conn, source)
    for owner in dict.fromkeys((membership, None)):
        for row in entries(conn, owner):
            if row['request_key'] == key:
                return row
    return None


def complete(conn, key, address, value, at, items, limit, previous):
    source, stream, request = address
    membership = _membership(conn, source)
    target = value['playlist_id']
    source_ref = membership
    target_ref = _membership(conn, target)
    if previous is not None:
        if membership is not None and all(r['id'] != previous['id'] for r in entries(conn, membership)):
            membership = None
        row = previous
    else:
        identity = conn.execute("SELECT coalesce(max(document->>'id'),0)+1 FROM (" + ROWS + ')', (UNRESOLVED,)).fetchone()[0]
        row = dict(id=identity, request_key=key, source_playlist_id=source, source_ref=source_ref,
                   source_resolution=None if source_ref else 'missing_playlist', input_stream=stream,
                   request_id=request, target_stream=value['stream'], created_at=None)
    row.update(status='complete', target_playlist_id=target, target_ref=target_ref,
               target_resolution=None if target_ref else 'missing_playlist', built_at=at, items=items)
    save(conn, membership, [r for r in entries(conn, membership) if r['id'] != row['id']] + [row])
    for owner, identity in conn.execute("SELECT membership,document->>'id' FROM (" + ROWS + ') '
            "WHERE document->>'status'='complete' ORDER BY document->>'id' DESC LIMIT -1 OFFSET ?",
            (UNRESOLVED, limit)).fetchall():
        save(conn, owner, [r for r in entries(conn, owner) if r['id'] != identity])


def read_all(conn):
    return (json.loads(raw) for raw, in conn.execute('SELECT document FROM (' + ROWS + ") "
        "WHERE document->>'status'='complete' ORDER BY document->>'id'", (UNRESOLVED,)))


def present(conn):
    return conn.execute('SELECT 1 FROM (' + ROWS + ") WHERE document->>'status'='complete' LIMIT 1",
                        (UNRESOLVED,)).fetchone() is not None


def clear(conn):
    if conn.execute('SELECT 1 FROM (' + ROWS + ") WHERE document->>'status'='pending' LIMIT 1", (UNRESOLVED,)).fetchone():
        raise ValueError('Cannot replace unconfirmed TV creations')
    conn.execute(f'UPDATE playlists SET {COLUMN}=NULL WHERE {COLUMN} IS NOT NULL')
    worker_settings.save(conn, UNRESOLVED, None)
