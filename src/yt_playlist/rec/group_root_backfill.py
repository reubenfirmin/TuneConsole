"""Compatibility entry points for bounded historical group-root inventory."""
from yt_playlist.controllers.group_root_backfill import (
    BATCH_ROWS, BATCH_BYTES, BATCH_SECONDS, MAX_RECORD_BYTES, KINDS, Owner, Proposal,
)
from yt_playlist.rec import group_roots


def Backfill(store):
    from yt_playlist.library.group_composition import backfill
    return backfill(group_roots.source(store), batch_rows=BATCH_ROWS, batch_bytes=BATCH_BYTES,
                    batch_seconds=BATCH_SECONDS, max_record_bytes=MAX_RECORD_BYTES)


def maintain(store):
    store = group_roots.source(store)
    if not group_roots.enabled(store):
        return False
    if not hasattr(store, '_group_root_backfill'):
        store._group_root_backfill = Backfill(store)
    return store._group_root_backfill.step()
