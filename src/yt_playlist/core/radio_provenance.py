"""Retained radio origins, including legacy provider IDs with unknown scope."""
import json

from . import playlist_scopes

UNRESOLVED = 'radio_unresolved_playlist_ids'

def ids(conn):
    return [row[0] for row in conn.execute('SELECT provider_playlist_id FROM playlist_scopes WHERE radio_origin=1 '
        'UNION SELECT p.value FROM settings s,json_each(s.value) p WHERE s.key=? ORDER BY 1', (UNRESOLVED,))]


def mark_scope(conn, scope):
    changed = conn.execute('UPDATE playlist_scopes SET radio_origin=1 WHERE id=? AND radio_origin=0', (scope,)).rowcount
    if not changed and conn.execute('SELECT 1 FROM playlist_scopes WHERE id=?', (scope,)).fetchone() is None:
        raise ValueError('Unknown Radio playlist scope')


def remember(conn,provider_id,account_id=None):
    if not isinstance(provider_id,str) or not provider_id:
        raise ValueError('Radio origin requires a provider playlist ID')
    if account_id is not None:
        scope = playlist_scopes.ensure(conn,provider_id,account_id)
    else:
        matches = list(conn.execute('SELECT id FROM playlist_scopes WHERE provider_playlist_id=?',(provider_id,)))
        scope = matches[0][0] if len(matches)==1 else None
    if scope is not None:
        mark_scope(conn, scope)
        return
    row = conn.execute('SELECT value FROM settings WHERE key=?', (UNRESOLVED,)).fetchone()
    retained = set(json.loads(row[0])) if row else set()
    if provider_id not in retained:
        retained.add(provider_id)
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (UNRESOLVED, json.dumps(sorted(retained))))
