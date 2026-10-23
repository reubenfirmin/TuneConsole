"""Immediate, bounded readback for Undo evidence; never filled in by a later sync."""
import logging
import time

from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.undo_conflicts import playlist_state

logger = logging.getLogger(__name__)


def capture_post(client, playlist_id, *, expected_videos, expected_metadata=None,
                 sleep=time.sleep):
    # A complete later read proves contents, not ownership. Never capture an
    # unconstrained baseline, including when a caller cannot establish its effects.
    if expected_videos is None:
        logger.warning("Undo capture playlist=%s operation=verify_owned_effects unavailable", playlist_id)
        return None
    expected = list(expected_videos)
    for attempt in range(1, 4):
        detail, stage = None, "get_playlist"
        try:
            detail = client.get_playlist(playlist_id, limit=None)
            stage = "validate_playlist"
            validate_playlist(detail)
            state = playlist_state(detail)
            stage = "verify_written_sequence"
            if state["videos"] != expected:
                raise ValueError("Readback does not match the confirmed writes")
            stage = "verify_written_metadata"
            if any(state.get(key) is not None and state[key] != value
                   for key, value in (expected_metadata or {}).items()):
                raise ValueError("Readback metadata does not match the created playlist")
            return state
        except Exception as exc:
            # No exception messages, response bodies, headers, titles or track IDs: parsers and
            # HTTP clients may embed credentials or entire payloads in their exception strings.
            tracks = detail.get("tracks") if isinstance(detail, dict) else None
            count = detail.get("trackCount") if isinstance(detail, dict) else None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning(
                "Undo capture playlist=%s operation=%s attempt=%d/3 exception=%s "
                "http_status=%s tracks=%s advertised=%s continuation=%s unidentified=%s "
                "error_response=%s unsuccessful_status=%s",
                playlist_id, stage, attempt, type(exc).__name__,
                status if type(status) is int else None,
                len(tracks) if isinstance(tracks, list) else None,
                count if type(count) is int else None,
                bool(isinstance(detail, dict) and
                     (detail.get("continuation") or detail.get("continuations"))),
                sum(not isinstance(t, dict) or not isinstance(t.get("videoId"), str)
                    or not t["videoId"].strip() for t in tracks)
                if isinstance(tracks, list) else None,
                bool(isinstance(detail, dict) and detail.get("error")),
                bool(isinstance(detail, dict) and
                     detail.get("status", "STATUS_SUCCEEDED") != "STATUS_SUCCEEDED"))
        if attempt < 3:
            sleep(0.5 * attempt)
    return None
