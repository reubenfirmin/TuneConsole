"""Resolve explicit release judgments without rewriting their original source evidence."""
import logging
import sqlite3

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.library.choice_composition import feedback
from yt_playlist.rec.album_feedback_state import state as state


log = logging.getLogger(__name__)


def set_feedback(store, browse, kind, now, opportunity_id=None, *, account_id=None):
    """Replace/clear every active alias judgment, retaining append-only responses."""
    feedback(store).replace(browse, kind, now, opportunity_id, account_id=account_id)
    # A queued intent survives admission contention, worker shutdown and a crash
    # after file publication. Never acknowledge an in-memory CaptureQueue entry.
    try_drain(store, limit=4)


def drain(store, *, limit=32):
    """Publish a bounded prefix without waiting for evidence admission or the SQL writer."""
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Invalid album feedback response batch size')
    if store.activity_paused or store._lock.held_by_current_thread:
        return 0
    # Admission may become busy after any preceding check. Apply this policy
    # to every later repository/projection/writer acquisition, without owning
    # ServingLock during evidence reads or durable file publication.
    try:
        with store._lock.nonblocking_admission():
            return _drain(store, limit=limit)
    except sqlite3.OperationalError as exc:
        # Translate an owned interruption before try_drain's retry warning.
        # Unrelated SQL failures must retain their diagnostic traceback.
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            check_cancellable_work()
        raise


def _drain(store, *, limit):
    return feedback(store).drain(limit=limit)


def try_drain(store, *, limit=32):
    try:
        return drain(store, limit=limit)
    except WorkClosing:
        raise
    except WorkBudgetExceeded:
        return 0
    except Exception:
        log.warning('Retained album feedback awaits evidence publication', exc_info=True)
        return 0
