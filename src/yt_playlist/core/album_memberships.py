"""Saved albums and each account's snapshot checkpoint.

Callers own the writer transaction.
"""
import math


def _writing(conn):
    if not conn.in_transaction:
        raise RuntimeError('Album membership changes require an owner transaction')


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Album observation time must be finite epoch seconds')
    return value


def revision(conn, account_id):
    row = conn.execute('SELECT saved_album_revision FROM accounts WHERE id=?', (account_id,)).fetchone()
    return row[0] if row else 0


def advance(conn, account_id=None):
    _writing(conn)
    query = 'UPDATE accounts SET saved_album_revision=saved_album_revision+1'
    if account_id is None:
        conn.execute(query)
    else:
        conn.execute(query + ' WHERE id=?', (account_id,))


def save(conn, album_id, account_id=None, *, source='youtube', saved_at=None):
    _writing(conn)
    if saved_at is not None:
        _clock(saved_at)
    if account_id is None:
        conn.execute('UPDATE albums SET unattributed_saved_source=?,unattributed_saved_at=? '
                     'WHERE id=? AND unattributed_saved_source IS NULL', (source, saved_at, album_id))
    else:
        conn.execute('INSERT INTO account_albums(account_id,album_id,source,saved_at) VALUES (?,?,?,?) '
                     'ON CONFLICT(account_id,album_id) DO NOTHING', (account_id, album_id, source, saved_at))


def remove(conn, album_id, account_id=None):
    _writing(conn)
    if account_id is None:
        conn.execute('UPDATE albums SET unattributed_saved_source=NULL,unattributed_saved_at=NULL '
                     'WHERE id=? AND unattributed_saved_source IS NOT NULL', (album_id,))
    else:
        conn.execute('DELETE FROM account_albums WHERE account_id=? AND album_id=?', (account_id, album_id))


def observe_snapshot(conn, account_id, at):
    """Advance the revision even when the completed snapshot is empty."""
    _writing(conn)
    at = _clock(at)
    conn.execute('UPDATE accounts SET saved_album_revision=saved_album_revision+1,saved_album_synced_at=? '
                 'WHERE id=?', (at, account_id))


def merge(conn, target, retired):
    """Rebind current membership on an independently proven release merge."""
    _writing(conn)
    if target == retired:
        return
    conn.execute('INSERT INTO account_albums(account_id,album_id,source,saved_at) '
                 'SELECT account_id,?,source,saved_at FROM account_albums WHERE album_id=? '
                 'ON CONFLICT(account_id,album_id) DO UPDATE SET saved_at='
                 'COALESCE(min(saved_at,excluded.saved_at),saved_at,excluded.saved_at)', (target, retired))
    conn.execute('DELETE FROM account_albums WHERE album_id=?', (retired,))
    old = conn.execute('SELECT unattributed_saved_source,unattributed_saved_at FROM albums WHERE id=?',
                       (retired,)).fetchone()
    if old[0] is not None:
        conn.execute('UPDATE albums SET unattributed_saved_source=COALESCE(unattributed_saved_source,?), '
                     'unattributed_saved_at=COALESCE(min(unattributed_saved_at,?),unattributed_saved_at,?) '
                     'WHERE id=?', (old[0], old[1], old[1], target))
        remove(conn, retired)
    advance(conn)
