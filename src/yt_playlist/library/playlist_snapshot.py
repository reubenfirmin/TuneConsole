"""Shared completeness contract for cached membership and remote mutations."""
import re

from ytmusicapi.exceptions import YTMusicServerError


def http_status(exc):
    """Read a structured HTTP status or ytmusicapi's fixed HTTP error prefix."""
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status is None and isinstance(exc, YTMusicServerError):
        match = re.match(r'Server returned HTTP (\d{3}):', str(exc))
        status = int(match[1]) if match else None
    return status


def playlist_if_present(client, playlist_id):
    """Only an explicit 404 proves absence; every present playlist must be complete."""
    try:
        detail = client.get_playlist(playlist_id, limit=None)
    except Exception as exc:
        if http_status(exc) == 404:
            return None
        raise
    error = detail.get('error') if isinstance(detail, dict) else None
    if isinstance(error, dict) and error.get('code') == 404:
        return None
    return validate_playlist(detail)


def is_complete_playlist(detail):
    """A readable page is authoritative only after all advertised items have arrived.

    Unavailable items can still occupy playlist positions. Mutations additionally
    require exact video IDs, checked by validate_playlist below.
    """
    if (not isinstance(detail, dict) or not isinstance(detail.get("tracks"), list)
            or any(not isinstance(t, dict) for t in detail["tracks"])
            or detail.get("error") or detail.get("continuation") or detail.get("continuations")
            or detail.get("status", "STATUS_SUCCEEDED") != "STATUS_SUCCEEDED"):
        return False
    count = detail.get("trackCount")
    audit = detail.get('_readAudit')
    if audit is not None and (not isinstance(audit, dict) or audit.get('exhausted') is not True
                              or audit.get('omitted') or audit.get('unidentified')):
        return False
    return count is None or (type(count) is int and count == len(detail["tracks"]))


def validate_playlist(detail):
    if not is_complete_playlist(detail):
        raise ValueError("YouTube did not return a complete playlist")
    tracks = detail["tracks"]
    if any(not isinstance(t, dict) or not isinstance(t.get("videoId"), str)
           or not t["videoId"].strip() for t in tracks):
        raise ValueError("YouTube returned an unidentified playlist item")
    return detail
