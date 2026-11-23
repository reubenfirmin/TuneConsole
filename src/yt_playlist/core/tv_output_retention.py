"""Retire disposable TV output without resolving artists or remote receipts."""
from . import tv_artwork_state
from .tv_recommendation_state import save_pool


def clear_disposable(conn):
    """Keep demand and retry receipts when catalog identity invalidates output."""
    if not conn.in_transaction:
        raise ValueError('TV invalidation requires its outer transaction')
    tv_artwork_state.clear_results(conn)
    save_pool(conn, None)
