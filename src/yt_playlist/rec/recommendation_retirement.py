"""Retire obsolete recommendations before admitting another live preparation round."""
import logging
import threading
import time

from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.group_admission import source
from yt_playlist.rec import group_roots, measurement, recommendation_roots
from yt_playlist.repos import recommendation_publication

log = logging.getLogger(__name__)
BATCH_ROWS = 32768
BATCH_POOLS = 512


def drain(store, now):
    """Occupy the worker, never the database lock, for the complete eligible backlog.

    Existing collectors preserve live roots and commit small durable prefixes.
    Read-only app admission pauses new mutations, not SQL reads or reader pins.
    A failure leaves recovery pending; the worker retries before other jobs.
    """
    store = source(store)
    if not recommendation_roots.enabled(store):
        return
    with store._lock:
        if not hasattr(store, '_recommendation_retirement_lock'):
            store._recommendation_retirement_lock = threading.Lock()
    if not store._recommendation_retirement_lock.acquire(blocking=False):
        raise WorkBudgetExceeded('Recommendation cleanup is already running')
    started = time.monotonic()
    progress = dict(generations=0, pools=0, rows=0, policies=0, bodies=0)
    try:
        # A dead reader can be the last owner hiding eligible work from the
        # probe. Reap its lifetime proof before deciding the gate is clear.
        while True:
            check_cancellable_work()
            group_roots.reap_pending(store)
            if not getattr(store, '_group_pending_after', ''):
                break
        if not (store.recommendations.retirement_pending()
                or store.rec_ledger.policy_retirement_pending(now)):
            store._recommendation_retirement_pending = False
            return
        store._recommendation_retirement_pending = True
        store._recommendation_retirement_progress = progress
        store._recommendation_retirement_error = ''
        checkpoint_at = started + 1
        while True:
            check_cancellable_work()
            generation = recommendation_publication.cleanup_generations(
                store.recommendations, checkpoint=False, row_limit=BATCH_ROWS)
            pool = recommendation_publication.cleanup_pools(
                store.recommendations, checkpoint=False, row_limit=BATCH_ROWS, pool_limit=BATCH_POOLS)
            for result in (generation, pool):
                progress['generations'] += result.generations
                progress['pools'] += result.pools
                progress['rows'] += result.rows
            if time.monotonic() >= checkpoint_at:
                store.recommendations.checkpoint_retirement()
                checkpoint_at = time.monotonic() + 1
                log.info('Recommendation cleanup progress: %s', progress)
            if not generation and not pool:
                break
        # Releasing old pool owners can make policies behind the previous
        # incremental cursor eligible. Start a complete fresh scan.
        store.rec_ledger.restart_policy_retirement()
        while True:
            check_cancellable_work()
            result = measurement.maintain_storage(store, now, progress=True)
            if not result['published']:
                raise WorkBudgetExceeded('Recommendation policy cleanup needs another attempt')
            progress['policies'] += result.get('policies_deleted', 0)
            progress['bodies'] += result.get('bodies_deleted', 0)
            if not result['pending']:
                break
        if (store.recommendations.retirement_pending()
                or store.rec_ledger.policy_retirement_pending(now)):
            raise WorkBudgetExceeded('Recommendation cleanup has more eligible work')
        store._recommendation_retirement_pending = False
        log.info('Recommendation cleanup complete: elapsed=%.3fs %s', time.monotonic() - started, progress)
        return dict(progress)
    except BaseException as error:
        store._recommendation_retirement_pending = True
        store._recommendation_retirement_error = type(error).__name__
        raise
    finally:
        try:
            if progress['rows'] or progress['pools'] or progress['generations']:
                store.recommendations.checkpoint_retirement()
        finally:
            store._recommendation_retirement_lock.release()
