"""Publish response effects from their originating album feedback rows."""
from contextlib import contextmanager
import time

from . import policy_documents
from .activity import WorkBudgetExceeded, check_cancellable_work
from .evidence_ledger import _mkdir, root_for_database


def _next_sequence(conn):
    return conn.execute('SELECT 1+max('
                        'coalesce((SELECT max(queued_feedback) FROM album_feedback WHERE queued_feedback IS NOT NULL),0),'
                        'coalesce((SELECT max(queued_withdrawal) FROM album_feedback WHERE queued_withdrawal IS NOT NULL),0))'
                        ).fetchone()[0]


def enqueue(conn, action_id, opportunity_id, kind, now, *, option_id=None,
            feedback_kind=None, target_action_id=None, sequence=None):
    """Capture the effect in the outer judgment transaction."""
    if not conn.in_transaction:
        raise RuntimeError('Album feedback capture requires its judgment transaction')
    original = target_action_id if kind == 'withdraw' else action_id
    row = conn.execute(
        'SELECT id,source_browse_id,response,created_at,queued_feedback,withdrawal_action_id '
        'FROM album_feedback WHERE action_id=? AND opportunity_id=? ORDER BY id LIMIT 1',
        (original, opportunity_id)).fetchone()
    if row is None:
        raise ValueError('Album feedback effect has no originating response')
    identity, browse, feedback, at, queued_feedback, withdrawal = row
    if kind == 'feedback':
        if (option_id, feedback_kind, now) != (browse, feedback, at):
            raise ValueError('Album feedback response ID was reused')
        if queued_feedback is None:
            conn.execute('UPDATE album_feedback SET queued_feedback=? WHERE id=?',
                         (sequence if sequence is not None else _next_sequence(conn), identity))
    elif kind == 'withdraw':
        if withdrawal is None:
            conn.execute('UPDATE album_feedback SET withdrawal_action_id=?,withdrawal_at=?,queued_withdrawal=? WHERE id=?',
                         (action_id, now, sequence if sequence is not None else _next_sequence(conn), identity))
        elif withdrawal != action_id:
            raise ValueError('Album feedback response ID was reused')
    else:
        raise ValueError('Unknown album feedback effect')


def pending(conn, *, after=0, limit=32):
    return conn.execute("""
        SELECT queued_feedback AS id,action_id,opportunity_id,'feedback' AS kind,created_at AS observed_at,
               source_browse_id AS option_id,response AS feedback_kind,NULL AS target_action_id,0 AS delivered
        FROM album_feedback WHERE queued_feedback>?
        UNION ALL
        SELECT queued_withdrawal,withdrawal_action_id,opportunity_id,'withdraw',withdrawal_at,
               NULL,NULL,action_id,0
        FROM album_feedback WHERE queued_withdrawal>?
        ORDER BY id LIMIT ?
    """, (after, after, limit)).fetchall()


def retained_opportunities(conn):
    return {row[0] for row in conn.execute('SELECT opportunity_id FROM album_feedback WHERE queued_feedback>0 '
                                          'UNION SELECT opportunity_id FROM album_feedback WHERE queued_withdrawal>0')}


def acknowledge(conn, action_id):
    # Keep the identity and first request time of a delivered withdrawal.
    conn.execute('UPDATE album_feedback SET queued_feedback=NULL WHERE action_id=? AND queued_feedback IS NOT NULL',
                 (action_id,))
    conn.execute('UPDATE album_feedback SET queued_withdrawal=NULL WHERE withdrawal_action_id=? AND queued_withdrawal IS NOT NULL',
                 (action_id,))


@contextmanager
def fence(store, *, wait_seconds=1):
    """Exclude final evidence retirement while capturing, without waiting under ServingLock.

    This independent OS fence permits capture while the evidence publisher is
    busy. The collector takes it only for its final pin check and deletion.
    """
    directory = root_for_database(store._db_path, store._evidence_key)
    _mkdir(directory)
    deadline = time.monotonic() + wait_seconds
    with (directory / '.album-feedback.lock').open('a+b') as stream:
        while True:
            check_cancellable_work()
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or store._lock.held_by_current_thread:
                raise WorkBudgetExceeded('Album feedback capture is busy')
            time.sleep(min(.01, remaining))
        try:
            yield
        finally:
            unlock()
