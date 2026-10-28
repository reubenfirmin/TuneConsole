"""Resolve explicit release judgments without rewriting their original source evidence."""
import json
import uuid


def state(store, browse_ids=None):
    """One judgment per verified release; latest time wins, then source ID breaks ties.

    Without a selection, return preferred IDs for feedback lists and taste inputs.
    A selection retains the caller's IDs, including unknown/unreconciled releases.
    Reading never changes the original feedback or its opportunity/action IDs.
    """
    original = json.loads(store.settings.get_setting('album_feedback') or '{}')
    if not original:
        return {}
    requested = None if browse_ids is None else set(browse_ids)
    preferred = store.collection.preferred_album_ids(set(original) | (requested or set()))
    resolved = {}
    for browse, item in sorted(original.items(), key=lambda row: (row[1]['at'], row[0])):
        resolved[preferred[browse]] = item
    if requested is None:
        return resolved
    return {browse: resolved[preferred[browse]] for browse in requested if preferred[browse] in resolved}


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
