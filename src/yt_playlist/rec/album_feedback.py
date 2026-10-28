"""Resolve explicit release judgments without rewriting their original source evidence."""
import json
import uuid

from yt_playlist.rec.album_feedback_state import state as state


def set_feedback(store, browse, kind, now, opportunity_id=None):
    """Replace/clear every active alias judgment, retaining append-only responses."""
    from yt_playlist.rec import choice_evidence
    if kind not in {'more', 'known', 'reject', 'clear'} or not browse:
        raise ValueError('Choose an album feedback action')
    with store._lock:
        original = json.loads(store.settings.get_setting('album_feedback') or '{}')
        preferred = store.collection.preferred_album_ids(set(original) | {browse})
        withdrawn = set()
        for source in list(original):
            if preferred[source] != preferred[browse]:
                continue
            previous = original.pop(source)
            opportunity, action = previous.get('opportunity_id'), previous.get('action_id')
            if opportunity and action and (opportunity, action) not in withdrawn:
                choice_evidence.safe_respond(store, opportunity, 'withdraw', now,
                    action_id=uuid.uuid4().hex, details={'action_id': action})
                withdrawn.add((opportunity, action))
        if kind != 'clear':
            action = uuid.uuid4().hex
            original[browse] = {'kind': kind, 'at': now, 'opportunity_id': opportunity_id, 'action_id': action}
            if opportunity_id:
                choice_evidence.safe_respond(store, opportunity_id, 'feedback', now, option_id=browse,
                                            action_id=action, details={'kind': kind})
        store.settings.set_setting('album_feedback', json.dumps(original))
