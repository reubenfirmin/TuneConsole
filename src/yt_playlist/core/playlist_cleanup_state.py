"""Retry state on a playlist scope, keyed by the captured account."""
import json
import math

from . import playlist_scopes


COLUMN = 'cleanup_attempts'
DECLARATION = f"{COLUMN} TEXT NOT NULL DEFAULT '{{}}' CHECK(json_type({COLUMN}) IS 'object')"
INDEX = 'ix_pending_playlist_cleanup'
PRESENT = f"{COLUMN}<>'{{}}'"


def _path(account):
    if type(account) is not int:
        raise ValueError('Cleanup requires a captured account ID')
    return f'$."{account}"'


def _entry(conn, account, remote):
    path = _path(account)
    return conn.execute(f"""SELECT id, json_extract({COLUMN}, ?) FROM playlist_scopes
        WHERE provider_playlist_id=? AND json_type({COLUMN}, ?)='object'""", (path, remote, path)).fetchone()


def read(conn, account, remote):
    row = _entry(conn, account, remote)
    return json.loads(row[1]) if row else None


def _entries(conn):
    return conn.execute(f"""SELECT CAST(item.key AS INTEGER), s.provider_playlist_id, item.value
        FROM playlist_scopes s JOIN json_each(s.{COLUMN}) item WHERE s.{PRESENT}""")


def states(conn):
    return {(account, remote): json.loads(value) for account, remote, value in _entries(conn)}


def deadlines(conn):
    return [(account, remote, json.loads(value)['next_retry_at']) for account, remote, value in _entries(conn)]


def save(conn, account, remote, value):
    entry = _entry(conn, account, remote)
    if value is None:
        if entry:
            conn.execute(f'UPDATE playlist_scopes SET {COLUMN}=json_remove({COLUMN},?) WHERE id=?',
                         (_path(account), entry[0]))
        return
    if not conn.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone():
        raise ValueError('New cleanup attempts require their retained account')
    scope = entry[0] if entry else playlist_scopes.ensure(conn, remote, account)
    put(conn, scope, account, remote, value)


def put(conn, scope, account, remote, value):
    if (not isinstance(value, dict) or type(value.get('identity')) is not int
            or value['identity'] != account or value.get('ytm') != remote
            or not isinstance(remote, str) or not remote):
        raise ValueError('Cleanup state disagrees with its captured target')
    if (type(value.get('attempts')) is not int or value['attempts'] < 1
            or value.get('phase') not in (None, 'reconcile', 'backup', 'delete')
            or any(type(value.get(key)) not in (int, float) or not math.isfinite(value[key])
                   for key in ('last_attempt_at', 'next_retry_at'))):
        raise ValueError('Invalid playlist cleanup attempt')
    if not conn.execute(f"""UPDATE playlist_scopes SET {COLUMN}=json_set({COLUMN},?,json(?))
        WHERE id=? AND provider_playlist_id=? AND (kind<>'account' OR account_id=?)""",
        (_path(account), json.dumps(value, allow_nan=False), scope, remote, account)).rowcount:
        raise ValueError('Cleanup state requires its original playlist scope')
