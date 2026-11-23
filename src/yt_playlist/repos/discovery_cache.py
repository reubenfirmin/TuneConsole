"""Publish already prepared discovery cache rows in the cache owner's transaction."""
def publish(conn, prepared):
    """Publish prepared values; the caller owns the guard and transaction."""
    conn.executemany('UPDATE rec_proposals SET payload=? WHERE surface=?', prepared)
