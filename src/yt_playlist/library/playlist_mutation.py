"""Verified, ordered playlist writes, independent of HTTP and radio session state."""
from dataclasses import dataclass
from collections import Counter, defaultdict, deque
from typing import Literal

from yt_playlist.library.playlist_writes import add_items_resilient
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
        order = list(range(len(tracks)))
        for index in range(len(target) - 1, -1, -1):
            item = target[index]
            successor = target[index + 1] if index + 1 < len(target) else None
            pos = order.index(item)
            if (successor is None and pos == len(order) - 1) or (
                    successor is not None and pos + 1 < len(order) and order[pos + 1] == successor):
                continue
            handle = tracks[item].get("setVideoId")
            next_handle = tracks[successor].get("setVideoId") if successor is not None else None
            if not handle or (successor is not None and not next_handle):
                raise ValueError("YouTube has not exposed playlist ordering handles. Retry Undo to continue.")
            result = client.edit_playlist(playlist_id, moveItem=(handle, next_handle)
                                          if successor is not None else handle)
            status = result.get("status") if isinstance(result, dict) else result
            if status is not None and status != "STATUS_SUCCEEDED":
                raise ValueError("YouTube rejected a playlist position change. Retry Undo to continue.")
            order.remove(item)
            order.insert(order.index(successor) if successor is not None else len(order), item)
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


def reconcile_playlist(client, playlist_id, desired_video_ids):
    """Apply an ordered queue and report only state confirmed by a fresh remote read.

    Readback matters even when the transport returns success: additions can skip unavailable
    uploads, removals can fail silently, and a set comparison would miss a wrong playback order.
    A failed final read never upgrades the requested queue to confirmed state.
    """
    desired = list(dict.fromkeys(v for v in desired_video_ids if v))
    skipped, error = [], None
    try:
        tracks = _read(client, playlist_id)
        current = {t["videoId"] for t in tracks}
        _, skipped = add_items_resilient(client, playlist_id,
                                        [v for v in desired if v not in current])
        seen, extras = set(), []
        for track in tracks:
            vid = track["videoId"]
            if vid not in desired or vid in seen:
                extras.append(track)
            seen.add(vid)
        if extras:
            client.remove_playlist_items(playlist_id, extras)
        tracks = _read(client, playlist_id)
        order = [t["videoId"] for t in tracks]
        handles = {t["videoId"]: t.get("setVideoId") for t in tracks}
        target = [v for v in desired if v in order]
        # Work from the end: moveItem places an upload immediately before its successor.
        for index in range(len(target) - 1, -1, -1):
            vid = target[index]
            successor = target[index + 1] if index + 1 < len(target) else None
            pos = order.index(vid)
            if (successor is None and pos == len(order) - 1) or (
                    successor is not None and pos + 1 < len(order) and order[pos + 1] == successor):
                continue
            if not handles.get(vid) or (successor and not handles.get(successor)):
                raise ValueError("YouTube did not return playlist ordering handles")
            client.edit_playlist(playlist_id, moveItem=(handles[vid], handles[successor])
                                 if successor else handles[vid])
            order.remove(vid)
            order.insert(order.index(successor) if successor else len(order), vid)
    except Exception as exc:  # the caller needs an honest result even after a partial write
        error = str(exc) or type(exc).__name__
    try:
        confirmed = tuple(t["videoId"] for t in _read(client, playlist_id))
    except Exception as exc:
        return PlaylistResult("failed", None, tuple(skipped), str(exc) or type(exc).__name__)
    missing = tuple(v for v in desired if v not in confirmed)
    if list(confirmed) == desired:
        return PlaylistResult("complete", confirmed)
    return PlaylistResult("partial", confirmed, missing,
                          error or "YouTube did not apply the complete ordered queue")
