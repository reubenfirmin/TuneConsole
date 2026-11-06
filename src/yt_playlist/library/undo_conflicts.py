"""Conservative three-way inversion of playlist edits using exact playback IDs."""
from collections import defaultdict


UNDO_NOTICES = {
    'copy_snapshot': 'The post-action copy snapshot could not be verified or was not saved. '
                     'The copy was preserved. Review it in YouTube Music before removing it manually.',
    'copy_changed': 'Undo conflict: the created playlist has changed since this action. '
                    'It was preserved. Review its edits in YouTube Music before retrying Undo.',
    'playlist_snapshot': 'The post-action snapshot could not be verified or was not saved. '
                         'The playlist was preserved. Compare it with the backup before recovering manually.',
    'track_replaced': 'Undo conflict: an added track was replaced after this action. '
                      'The playlist was preserved. Review it in YouTube Music.',
    'occurrences_changed': 'Undo conflict: repeated playlist occurrences changed after this action. '
                           'The playlist was preserved. Review it in YouTube Music.',
    'track_readded': 'Undo conflict: a removed track was re-added after this action. '
                     'The playlist was preserved. Review it in YouTube Music.',
    'order_changed': 'Undo conflict: playlist order changed after this action. '
                     'The playlist was preserved. Review its order before retrying Undo.',
}


class UndoConflict(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(UNDO_NOTICES[reason])


def playlist_state(detail):
    return {"videos": [t["videoId"] for t in detail["tracks"]],
            "handles": [t.get("setVideoId") for t in detail["tracks"]],
            "title": detail.get("title"), "description": detail.get("description")}


def require_unchanged_copy(expected, current):
    if expected is None:
        raise UndoConflict('copy_snapshot')
    changed = expected["videos"] != current["videos"]
    for field in ("title", "description"):
        changed |= expected.get(field) is not None and expected[field] != current.get(field)
    changed |= any(old and old != new for old, new in
                   zip(expected.get("handles", []), current.get("handles", [])))
    if changed:
        raise UndoConflict('copy_changed')


def keeper_target(entry, current):
    before = entry.get("prev", [])
    after = entry.get("post")
    videos = current["videos"]
    if after is None:
        raise UndoConflict('playlist_snapshot')
    # An item removed and re-added later is a different occurrence, even if its video matches.
    post = entry.get("post_state") or {}
    affected = set(after) - set(before)
    handles = dict(zip(post.get("videos", []), post.get("handles", [])))
    if any(vid in affected and handles.get(vid) and handle != handles[vid]
           for vid, handle in zip(videos, current.get("handles", []))):
        raise UndoConflict('track_replaced')
    if videos == after:
        return list(before)
    # Repeated occurrences and overlapping additions have no unambiguous inverse without an
    # item-by-item edit history. Leave them intact for an explicit user decision.
    if any(len(seq) != len(set(seq)) for seq in (before, after, videos)):
        raise UndoConflict('occurrences_changed')
    prior, expected, actual = set(before), set(after), set(videos)
    later_additions = actual - expected
    if later_additions & prior:
        raise UndoConflict('track_readded')
    if [v for v in videos if v in expected] != [v for v in after if v in actual]:
        raise UndoConflict('order_changed')
    later_removals = expected - actual
    base = [v for v in before if v not in later_removals]
    base_set = set(base)
    additions = defaultdict(list)
    anchor = None
    for vid in videos:
        if vid in base_set:
            anchor = vid
        elif vid in later_additions:
            additions[anchor].append(vid)
    result = list(additions[None])
    for vid in base:
        result.append(vid)
        result.extend(additions[vid])
    return result
