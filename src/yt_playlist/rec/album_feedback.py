"""Resolve explicit release judgments without rewriting their original source evidence."""
import json
import time
import uuid

from yt_playlist.core import album_feedback_storage
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec.album_feedback_state import state as state


def _respond(store, opportunity, kind, now, **kwargs):
    """Retry transient evidence admission with the same immutable response ID."""
    from yt_playlist.rec import choice_evidence
    deadline = time.monotonic() + 1
    while True:
        check_cancellable_work()
        result = choice_evidence.safe_respond(store, opportunity, kind, now, **kwargs)
        if (result is not None or store.activity_paused or store._lock.held_by_current_thread
                or time.monotonic() >= deadline):
            return result
        time.sleep(.01)


def set_feedback(store, browse, kind, now, opportunity_id=None, *, account_id=None):
    """Replace/clear every active alias judgment, retaining append-only responses."""
    if kind not in {'more', 'known', 'reject', 'clear'} or not browse:
        raise ValueError('Choose an album feedback action')
    with atomic_write(store):
        if album_feedback_storage.available(store.conn):
            with store.conn:
                previous,action = album_feedback_storage.replace(store.conn,browse,kind,now,
                    opportunity_id=opportunity_id,account_id=account_id)
        else:
            original = json.loads(store.settings.get_setting('album_feedback') or '{}')
            preferred = store.collection.preferred_album_ids(set(original) | {browse})
            previous = []
            for source in list(original):
                if preferred[source] == preferred[browse]:
                    previous.append(original.pop(source))
            action = uuid.uuid4().hex if kind != 'clear' else None
            if action:
                original[browse] = {'kind': kind, 'at': now, 'opportunity_id': opportunity_id, 'action_id': action}
            store.settings.set_setting('album_feedback', json.dumps(original))
    # Admission retries never emit evidence. Finish the SQL unit before append
    # I/O so a failed mutation cannot publish its response or hold the writer
    # reservation while evidence files are written.
    withdrawn = set()
    ledger = store.rec_ledger.evidence()
    for row in previous:
        row = dict(row)
        opportunity, original_action = row.get('opportunity_id'), row.get('action_id')
        if opportunity and original_action and (opportunity, original_action) not in withdrawn:
            withdrawal = uuid.uuid5(uuid.NAMESPACE_URL, 'album-feedback-withdrawal:' + str(original_action)).hex
            if ledger.get('response', withdrawal) is None:
                _respond(store, opportunity, 'withdraw', now,
                    action_id=withdrawal, details={'action_id': original_action})
            withdrawn.add((opportunity, original_action))
    if action and opportunity_id:
        _respond(store, opportunity_id, 'feedback', now, option_id=browse,
                 action_id=action, details={'kind': kind})
