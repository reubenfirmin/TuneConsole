"""Retire disposable TV output without resolving artists or remote receipts."""


def clear_disposable(conn):
    """Keep demand and retry receipts when catalog identity invalidates output."""
    if not conn.in_transaction:
        raise ValueError('TV invalidation requires its outer transaction')
    for table in ('tv_pool_items', 'tv_pool_attempts', 'tv_pool_state', 'tv_artist_artwork', 'tv_artwork_state'):
        conn.execute(f'DELETE FROM {table}')
