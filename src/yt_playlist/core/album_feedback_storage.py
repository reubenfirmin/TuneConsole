"""Individual release responses; callers own the transaction and serving lock."""
import math
from uuid import uuid4

from . import albums, collection_recovery


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='album_feedback'").fetchone() is not None


def active_account(conn):
    row = conn.execute('SELECT id FROM accounts WHERE active=1 AND is_master=1').fetchone()
    return row[0] if row else None


def rows(conn, *, account_id=None):
    """Unattributed historical responses remain visible alongside this account."""
    account_id = active_account(conn) if account_id is None else account_id
    return list(conn.execute('SELECT f.* FROM album_feedback f WHERE withdrawn_at IS NULL '
        'AND (account_id=? OR (account_id IS NULL AND NOT EXISTS('
        'SELECT 1 FROM album_feedback_legacy_dismissals d WHERE d.account_id=? AND d.album_id=f.album_id))) '
        'ORDER BY created_at,source_browse_id,id',(account_id,account_id)))


@collection_recovery.pinned_read
def original(conn, *, account_id=None):
    return {row['source_browse_id']: {'kind':row['response'],'at':row['created_at'],
                'opportunity_id':row['opportunity_id'],'action_id':row['action_id']}
            for row in rows(conn,account_id=account_id)}


def replace(conn, browse_id, response, now, *, opportunity_id=None, account_id=None):
    """Withdraw one release's current response, retaining every previous judgment."""
    if type(now) not in (int,float) or not math.isfinite(now):
        raise ValueError('Feedback requires a finite observation time')
    account_id = active_account(conn) if account_id is None else account_id
    if account_id is None:
        raise ValueError('Choose an active account before giving album feedback')
    if not conn.execute('SELECT 1 FROM accounts WHERE id=? AND active=1',(account_id,)).fetchone():
        raise ValueError('Album feedback requires an active account')
    conn.execute('UPDATE album_feedback SET withdrawn_at=withdrawn_at WHERE 0')
    album_id = albums.observe(conn,browse_id)
    previous = list(conn.execute('SELECT * FROM album_feedback WHERE album_id=? '
        'AND account_id=?',(album_id,account_id)))
    if any(row['created_at']>now for row in previous if row['withdrawn_at'] is None):
        raise ValueError('Feedback precedes the retained response')
    conn.execute('UPDATE album_feedback SET withdrawn_at=? WHERE album_id=? AND withdrawn_at IS NULL '
                 'AND account_id=?',(now,album_id,account_id))
    conn.execute('INSERT INTO album_feedback_legacy_dismissals VALUES (?,?,?) '
                 'ON CONFLICT(account_id,album_id) DO UPDATE SET dismissed_at=excluded.dismissed_at',
                 (account_id,album_id,now))
    action_id = None
    if response != 'clear':
        action_id = uuid4().hex
        conn.execute('INSERT INTO album_feedback(account_id,album_id,source_browse_id,response,created_at,'
            'opportunity_id,action_id) VALUES (?,?,?,?,?,?,?)',
            (account_id,album_id,browse_id,response,now,opportunity_id,action_id))
    return previous,action_id


def merge(conn, target, retired, now):
    """Keep all response lineage when verified provider releases become one."""
    current = list(conn.execute('SELECT * FROM album_feedback WHERE album_id IN (?,?) '
        'AND withdrawn_at IS NULL ORDER BY created_at,source_browse_id,id',(target,retired)))
    winners = {row['account_id']:row['id'] for row in current}
    for row in current:
        if winners[row['account_id']] != row['id']:
            conn.execute('UPDATE album_feedback SET withdrawn_at=? WHERE id=?',
                         (max(now,row['created_at']),row['id']))
    conn.execute('UPDATE album_feedback SET album_id=? WHERE album_id=?',(target,retired))
    conn.execute('INSERT INTO album_feedback_legacy_dismissals(account_id,album_id,dismissed_at) '
        'SELECT account_id,?,dismissed_at FROM album_feedback_legacy_dismissals WHERE album_id=? '
        'ON CONFLICT(account_id,album_id) DO UPDATE SET dismissed_at=MAX(dismissed_at,excluded.dismissed_at)',
        (target,retired))
    conn.execute('DELETE FROM album_feedback_legacy_dismissals WHERE album_id=?',(retired,))
