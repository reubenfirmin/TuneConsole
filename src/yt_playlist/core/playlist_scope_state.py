"""Explicit curation attributes of an already resolved durable playlist scope.

Provider/account resolution belongs to playlist_scopes. These functions accept
scope IDs and never reinterpret one as a provider identifier.
"""
import math


MERGE_DISMISSALS = 'playlist_merge_dismissals'


def available(conn):
    return any(row[1] == 'group_name' for row in conn.execute('PRAGMA table_info(playlist_scopes)'))


def _writing(conn):
    if not conn.in_transaction:
        raise RuntimeError('Scope state changes require an owner transaction')


def _flag(value):
    if type(value) not in (bool, int) or value not in (0, 1):
        raise ValueError('Playlist flags must be boolean or integer 0/1')
    return int(value)


def provenance(conn, scope_id):
    row = conn.execute('SELECT origin,adopted,provenance_description description FROM playlist_scopes '
                        'WHERE id=? AND origin IS NOT NULL', (scope_id,)).fetchone()
    return dict(row) if row else {'origin': 'ordinary', 'adopted': 1, 'description': None}


def set_provenance(conn, scope_id, *, origin, adopted, description=None):
    _writing(conn)
    adopted = _flag(adopted)
    if origin not in ('ordinary', 'tuneconsole'):
        raise ValueError('Invalid playlist origin')
    if description is not None and not isinstance(description, str):
        raise ValueError('Playlist description must be text or unknown')
    conn.execute('UPDATE playlist_scopes SET origin=?,adopted=?,provenance_description=? '
        'WHERE id=? AND (origin IS NOT ? OR adopted IS NOT ? OR provenance_description IS NOT ?)',
        (origin, adopted, description, scope_id, origin, adopted, description))


def adopt(conn, scope_id, description):
    _writing(conn)
    if not isinstance(description, str):
        raise ValueError('Playlist description must be text')
    conn.execute("UPDATE playlist_scopes SET adopted=1,provenance_description=? WHERE id=? AND origin='tuneconsole' "
        'AND (adopted IS NOT 1 OR provenance_description IS NOT ?)', (description, scope_id, description))


def set_group(conn, scope_id, name):
    _writing(conn)
    if name is not None and not isinstance(name, str):
        raise ValueError('Playlist group must be text or absent')
    conn.execute('UPDATE playlist_scopes SET group_name=? WHERE id=? AND group_name IS NOT ?', (name, scope_id, name))


def set_hidden(conn, scope_id, hidden):
    _writing(conn)
    hidden = _flag(hidden)
    conn.execute('UPDATE playlist_scopes SET hidden=? WHERE id=? AND hidden IS NOT ?', (hidden, scope_id, hidden))


def ignore_overlap(conn, scope_id, at):
    _writing(conn)
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('Overlap ignore time must be finite epoch seconds or unknown')
    # Repeat dismissal preserves the original observation, including an unknown
    # time; it is not a new decision merely because the process restarted.
    conn.execute('UPDATE playlist_scopes SET overlap_ignored=1,overlap_ignored_at=? '
                 'WHERE id=? AND overlap_ignored=0', (at, scope_id))


def unignore_overlap(conn, scope_id):
    _writing(conn)
    conn.execute('UPDATE playlist_scopes SET overlap_ignored=0,overlap_ignored_at=NULL '
                 'WHERE id=? AND overlap_ignored=1', (scope_id,))


def groups(conn):
    return {row['id']: row['group_name'] for row in conn.execute(
        'SELECT id,group_name FROM playlist_scopes WHERE group_name IS NOT NULL')}


def hidden(conn):
    return {row[0] for row in conn.execute('SELECT id FROM playlist_scopes WHERE hidden=1')}


def overlap_ignored(conn):
    return {row[0] for row in conn.execute('SELECT id FROM playlist_scopes WHERE overlap_ignored=1')}
