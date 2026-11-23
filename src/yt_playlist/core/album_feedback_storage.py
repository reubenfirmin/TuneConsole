"""Individual release responses; callers own the transaction and serving lock."""
from . import collection_recovery


def active_account(conn):
    row = conn.execute('SELECT id FROM accounts WHERE active=1 AND is_master=1').fetchone()
    return row[0] if row else None


def rows(conn, *, account_id=None):
    """Unattributed historical responses remain visible alongside this account."""
    account_id = active_account(conn) if account_id is None else account_id
    return list(conn.execute('SELECT f.* FROM album_feedback f WHERE withdrawn_at IS NULL '
        'AND (account_id=? OR (account_id IS NULL AND NOT EXISTS('
        'SELECT 1 FROM album_feedback own WHERE own.account_id=? AND own.album_id=f.album_id))) '
        'ORDER BY created_at,source_browse_id,id',(account_id,account_id)))


@collection_recovery.pinned_read
def original(conn, *, account_id=None):
    return {row['source_browse_id']: {'kind':row['response'],'at':row['created_at'],
                'opportunity_id':row['opportunity_id'],'action_id':row['action_id']}
            for row in rows(conn,account_id=account_id)}


def save_response(conn, album_id, account_id, browse_id, response, now, opportunity_id, action_id):
    conn.execute('UPDATE album_feedback SET withdrawn_at=? WHERE album_id=? AND withdrawn_at IS NULL '
                 'AND account_id=?',(now,album_id,account_id))
    if action_id is not None:
        conn.execute('INSERT INTO album_feedback(account_id,album_id,source_browse_id,response,created_at,'
            'opportunity_id,action_id) VALUES (?,?,?,?,?,?,?)',
            (account_id,album_id,browse_id,response,now,opportunity_id,action_id))
    else:
        # A first clear suppresses the unattributed response for this account.
        # Withdrawn account responses already carry that same decision.
        conn.execute('INSERT INTO album_feedback(account_id,album_id,source_browse_id,response,created_at,withdrawn_at) '
            "SELECT ?,?,?,'clear',?,? WHERE NOT EXISTS (SELECT 1 FROM album_feedback WHERE account_id=? AND album_id=?)",
            (account_id,album_id,browse_id,now,now,account_id,album_id))
