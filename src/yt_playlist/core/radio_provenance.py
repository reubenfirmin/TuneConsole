"""Retained radio origins, including legacy provider IDs with unknown scope."""
import json

from . import collection_recovery, entity_names, playlist_scopes

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
    scopes = entity_names.owner(conn, 'playlist_scopes')
    return [row[0] for row in conn.execute(f'SELECT DISTINCT COALESCE(s.{scopes.column("provider_playlist_id")},o.unresolved_provider_id) '
        f'FROM radio_playlist_origins o LEFT JOIN {scopes.table} s ON s.{scopes.column("id")}=o.playlist_scope_id ORDER BY 1')]


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
        scope_key,remote,_ = entity_names.scope_columns(conn)
        matches = list(conn.execute(f'SELECT {scope_key} FROM playlist_scopes WHERE {remote}=?',(provider_id,)))
        scope = matches[0][0] if len(matches)==1 else None
    conn.execute('INSERT OR IGNORE INTO radio_playlist_origins(playlist_scope_id,unresolved_provider_id) VALUES (?,?)',
                 (scope,None if scope is not None else provider_id))


def import_legacy(conn,values):
    selected = json.loads(values.get(KEYS[0]) or '[]')
    if not isinstance(selected,list):
        raise ValueError('Legacy radio origins must be a list')
    for value in [*selected,*(values[key] for key in KEYS[1:] if values.get(key))]:
        remember(conn,value)
