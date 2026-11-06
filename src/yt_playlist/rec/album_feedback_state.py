"""Read resolved release judgments without depending on evidence-writing policy."""


def state(store, browse_ids=None):
    """One judgment per verified release; latest time wins, then source ID breaks ties.

    Without a selection, return preferred IDs for feedback lists and taste inputs.
    A selection retains the caller's IDs, including unknown/unreconciled releases.
    Reading never changes the original feedback or its opportunity/action IDs.
    """
    if getattr(store._lock, 'held_by_current_thread', False):
        return _state(store, browse_ids)
    with store.read_view() as reader:
        return _state(reader, browse_ids)


def _state(store, browse_ids):
    original = store.album_feedback.original()
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
