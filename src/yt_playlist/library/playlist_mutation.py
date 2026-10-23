"""Verified, ordered playlist writes, independent of HTTP and radio session state."""
from dataclasses import dataclass
from collections import Counter, defaultdict, deque
from typing import Literal

from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.util.retry import with_retry


@dataclass(frozen=True)
class PlaylistResult:
    status: Literal["complete", "partial", "failed"]
    confirmed_vids: tuple | None      # None means the final remote state is unknown
    skipped: tuple = ()
    error: str | None = None

    @property
    def complete(self):
        return self.status == "complete"

    def diagnostics(self):
        return {"status": self.status, "skipped": list(self.skipped), "error": self.error}


def _read(client, playlist_id):
    detail = with_retry(lambda: client.get_playlist(playlist_id, limit=None))
    return validate_playlist(detail)["tracks"]


def mutation_status(result):
    """The two supported acknowledgement shapes; absent/unknown shapes require readback."""
    if isinstance(result, dict):
        if result.get("error"):
            return "STATUS_FAILED"
        result = result.get("status")
    if result is False:
        return "STATUS_FAILED"
    return result if isinstance(result, str) else None


def same_playlist_items(actual, expected):
    """Compare ordered occurrences, including every known playlist-scoped item handle."""
    return len(actual) == len(expected) and all(
        item["videoId"] == before["videoId"] and (
            not before.get("setVideoId") or item.get("setVideoId") == before["setVideoId"])
        for item, before in zip(actual, expected))


def confirm_playlist_write(write, read, matches, operation):
    """Issue a write once, then reconcile a missing/lost acknowledgement with one readback."""
    error = None
    try:
        result = write()
    except Exception as exc:
        error, result = exc, None
    status = mutation_status(result)
    if status == "STATUS_SUCCEEDED":
        return
    if status is not None:
        raise ValueError(f"YouTube rejected a playlist {operation}. Re-sync before trying again.")
    try:
        confirmed = matches(read())
    except Exception as exc:
        raise ValueError(f"YouTube did not confirm a playlist {operation}; its outcome is unconfirmed. "
                         "Re-sync before trying again; "
                         "the write was not retried.") from exc
    if not confirmed:
        raise ValueError(f"YouTube did not confirm a playlist {operation}; its outcome is unconfirmed. "
                         "Re-sync before trying again; "
                         "the write was not retried.") from error


def apply_playlist_permutation(client, playlist_id, tracks, target, *, movable=None):
    """Execute a caller-built permutation of the current occurrence indices.

    Callers own membership and target construction, and verify their final ordering contract.
    ``movable`` restricts writes to selected occurrences without changing the full target order.
    An absent acknowledgement requires readback before advancing the working order; ambiguous
    writes are never retried with potentially stale handles.
    """
    target = list(target)
    order = list(range(len(tracks)))
    if sorted(target) != order:
        raise ValueError("Playlist target must contain each occurrence exactly once")
    movable = set(order) if movable is None else set(movable)
    if not movable <= set(order):
        raise ValueError("Movable playlist occurrences must belong to the current playlist")
    for index in range(len(target) - 1, -1, -1):
        item = target[index]
        if item not in movable:
            continue
        successor = target[index + 1] if index + 1 < len(target) else None
        position = order.index(item)
        if (successor is None and position == len(order) - 1) or (
                successor is not None and position + 1 < len(order) and order[position + 1] == successor):
            continue
        handle = tracks[item].get("setVideoId")
        following = tracks[successor].get("setVideoId") if successor is not None else None
        if not handle or (successor is not None and not following):
            raise ValueError("YouTube has not exposed playlist ordering handles. Retry to continue.")
        expected = [occurrence for occurrence in order if occurrence != item]
        expected.insert(expected.index(successor) if successor is not None else len(expected), item)
        confirm_playlist_write(
            lambda: client.edit_playlist(playlist_id, moveItem=(handle, following)
                                         if successor is not None else handle),
            lambda: _read(client, playlist_id),
            lambda actual: same_playlist_items(actual, [tracks[occurrence] for occurrence in expected]),
            "position change")
        order = expected


def restore_playlist_sequence(client, playlist_id, desired_video_ids):
    """Restore exact playback IDs, order and repeated occurrences from an Undo snapshot.

    Unlike the radio queue's unique-song contract, recovery preserves occurrences. Positional
    writes use current setVideoId handles, and success always requires a complete readback.
    """
    desired = list(desired_video_ids)
    error = None
    try:
        tracks = _read(client, playlist_id)
        wanted = Counter(desired)
        present = Counter(t["videoId"] for t in tracks)
        remaining = wanted - present
        additions = []
        for vid in desired:
            if remaining[vid]:
                additions.append(vid)
                remaining[vid] -= 1
        if additions:
            # YouTube otherwise rejects the entire batch when adding a repeated occurrence.
            if any(present[v] or wanted[v] > 1 for v in additions):
                client.add_playlist_items(playlist_id, additions, duplicates=True)
            else:
                client.add_playlist_items(playlist_id, additions)
        tracks = _read(client, playlist_id)
        kept = Counter()
        extras = []
        for track in tracks:
            vid = track["videoId"]
            if kept[vid] < wanted[vid]:
                kept[vid] += 1
            else:
                if wanted[vid] and not track.get("setVideoId"):
                    raise ValueError("YouTube has not exposed the repeated item's removal handle")
                extras.append(track)
        if extras:
            client.remove_playlist_items(playlist_id, extras)
        tracks = _read(client, playlist_id)
        if Counter(t["videoId"] for t in tracks) != wanted:
            raise ValueError("YouTube did not restore all playlist occurrences. Retry Undo to continue.")
        # Identify occurrences separately even when several share the same playback ID.
        occurrences = defaultdict(deque)
        for index, track in enumerate(tracks):
            occurrences[track["videoId"]].append(index)
        target = [occurrences[vid].popleft() for vid in desired]
        apply_playlist_permutation(client, playlist_id, tracks, target)
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    try:
        confirmed = tuple(t["videoId"] for t in _read(client, playlist_id))
    except Exception as exc:
        return PlaylistResult("failed", None, error=str(exc) or type(exc).__name__)
    if list(confirmed) == desired:
        return PlaylistResult("complete", confirmed)
    return PlaylistResult("partial", confirmed, error=error or
                          "YouTube did not restore the saved playlist order. Retry Undo to continue.")
