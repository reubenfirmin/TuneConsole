"""Publish already prepared discovery cache rows in the cache owner's transaction."""
from yt_playlist.repos import proposal_cache


def publish(conn, prepared):
    """Publish prepared values; the caller owns the guard and transaction."""
    for value, surface in prepared:
        if isinstance(value, proposal_cache.PreparedCache):
            proposal_cache.finish(conn, value)
        else:
            conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?', (value, surface))
