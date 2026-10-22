"""Shared completeness contract for cached membership and remote mutations."""


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
    return count is None or (type(count) is int and count == len(detail["tracks"]))


def validate_playlist(detail):
    if not is_complete_playlist(detail):
        raise ValueError("YouTube did not return a complete playlist")
    tracks = detail["tracks"]
    if any(not isinstance(t, dict) or not isinstance(t.get("videoId"), str)
           or not t["videoId"].strip() for t in tracks):
        raise ValueError("YouTube returned an unidentified playlist item")
    return detail
