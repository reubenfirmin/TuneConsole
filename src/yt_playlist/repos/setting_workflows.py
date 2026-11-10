"""SQL manifests for account workflow settings."""
from yt_playlist.core import workflow_settings as workflows


def documents(conn, kind, account, target=None):
    if kind == 'gc':
        return [dict(row) for row in conn.execute('SELECT * FROM playlist_gc_attempts '
            'WHERE captured_account_id=? AND provider_playlist_id_snapshot=?', (account, target))]
    rows = [dict(row) for row in conn.execute('SELECT * FROM spotify_transfer_receipts '
        'WHERE source_kind=? AND captured_account_id IS ? ORDER BY id', (kind, account))]
    for row in rows:
        row['slots'] = [item[0] for item in conn.execute('SELECT video_id_snapshot FROM spotify_transfer_items '
            'WHERE receipt_id=? ORDER BY position', (row['id'],))]
    return rows


def keys(conn, prefix):
    result = [workflows._gc_key(account, remote) for account, remote in conn.execute(
        'SELECT captured_account_id,provider_playlist_id_snapshot FROM playlist_gc_attempts ORDER BY id')]
    result.extend((workflows.PLAYLIST if kind == 'playlist' else workflows.ALBUM)+('' if account is None else ':'+str(account))
                  for kind, account in conn.execute('SELECT DISTINCT source_kind,captured_account_id FROM spotify_transfer_receipts'))
    return [key for key in result if key.startswith(prefix)]


def register(conn, plans):
    revision = None
    for kind, prepared, reference in plans:
        if kind == 'gc':
            workflows.register_gc(conn, prepared, reference)
        elif kind == 'receipt':
            revision = workflows.register_receipt(conn, prepared, reference)
        else:
            source, account = prepared
            conn.execute('DELETE FROM spotify_transfer_receipts WHERE source_kind=? AND captured_account_id IS ?', (source, account))
    return revision
