"""Retained radio origins, including legacy provider IDs with unknown scope."""
import json

from . import collection_recovery, playlist_scopes

KEYS = ('radio_session_playlist_ids','radio_playlist_ytm','radio_playlist_a_ytm','radio_playlist_b_ytm')


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_playlist_origins'").fetchone() is not None


@collection_recovery.pinned_read
def ids(conn):
    if not available(conn):
        from . import radio_reads
        revision = radio_reads.origin_selected(conn)
        if revision is not None:
            return radio_reads.origin_ids(conn,revision)
        values = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?,?)',KEYS))
        return list(dict.fromkeys([*json.loads(values.get(KEYS[0]) or '[]'),
                                  *(values[key] for key in KEYS[1:] if values.get(key))]))
    return [row[0] for row in conn.execute('SELECT DISTINCT COALESCE(s.provider_playlist_id,o.unresolved_provider_id) '
        'FROM radio_playlist_origins o LEFT JOIN playlist_scopes s ON s.id=o.playlist_scope_id ORDER BY 1')]


def remember(conn,provider_id,account_id=None):
    if not isinstance(provider_id,str) or not provider_id:
        raise ValueError('Radio origin requires a provider playlist ID')
    if not available(conn):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        values = ids(conn)
        conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (KEYS[0],json.dumps(list(dict.fromkeys([*values,provider_id])))))
        return
    if account_id is not None:
        scope = playlist_scopes.ensure(conn,provider_id,account_id)
    else:
        matches = list(conn.execute('SELECT id FROM playlist_scopes WHERE provider_playlist_id=?',(provider_id,)))
        scope = matches[0][0] if len(matches)==1 else None
    conn.execute('INSERT OR IGNORE INTO radio_playlist_origins(playlist_scope_id,unresolved_provider_id) VALUES (?,?)',
                 (scope,None if scope is not None else provider_id))
