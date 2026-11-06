"""Resolve explicit release judgments without rewriting their original source evidence."""
import logging

from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.rec.album_feedback_state import state as state


log = logging.getLogger(__name__)


def set_feedback(store, browse, kind, now, opportunity_id=None, *, account_id=None):
    """Replace/clear every active alias judgment, retaining append-only responses."""
    if kind not in {'more', 'known', 'reject', 'clear'} or not browse:
        raise ActionInputError('album_feedback_required')
    store.album_feedback.replace(browse, kind, now, opportunity_id, account_id=account_id)
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
    with store._lock.nonblocking_admission():
        return _drain(store, limit=limit)


def _drain(store, *, limit):
    from yt_playlist.rec import choice_evidence
    after = getattr(store, '_album_feedback_response_after', 0)
    rows = store.album_feedback.pending_responses(after=after, limit=limit)
    completed = 0
    for row in rows:
        check_cancellable_work()
        if store.activity_paused:
            break
        # Retain every failed attempt, but let the next bounded pass visit
        # independent judgments before wrapping back to an unavailable offer.
        store._album_feedback_response_after = row['id']
        details = ({'kind': row['feedback_kind']} if row['kind'] == 'feedback'
                   else {'action_id': row['target_action_id']})
        # This bypasses only volatile queuing. The ordinary issued-menu, time,
        # option, immutable-ID and cross-process publication checks still run.
        result = choice_evidence.respond(store, row['opportunity_id'], row['kind'], row['observed_at'],
            option_id=row['option_id'], action_id=row['action_id'], details=details, synchronous=True)
        if result is None:
            break
        stored = store.rec_ledger.evidence().get('response', row['action_id'])
        if stored is not None:
            if (stored['opportunity_id'], stored['kind'], stored['option_id'], stored['details']) != (
                    row['opportunity_id'], row['kind'], row['option_id'], details):
                raise ValueError('Published album feedback differs from its durable intent')
            if row['kind'] == 'feedback' and stored['at'] != row['observed_at']:
                raise ValueError('Published album feedback differs from its durable intent')
            store.album_feedback.acknowledge_response(row['action_id'])
            completed += 1
    return completed


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
