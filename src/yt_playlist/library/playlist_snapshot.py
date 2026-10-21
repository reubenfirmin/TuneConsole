"""Validate authoritative, complete playlist reads before mutations or recovery snapshots."""


def validate_playlist(detail):
    if (not isinstance(detail, dict) or not isinstance(detail.get("tracks"), list)
            or detail.get("error") or detail.get("continuation") or detail.get("continuations")
            or detail.get("status", "STATUS_SUCCEEDED") != "STATUS_SUCCEEDED"):
        raise ValueError("YouTube did not return a complete playlist")
    tracks = detail["tracks"]
    if any(not isinstance(t, dict) or not isinstance(t.get("videoId"), str)
           or not t["videoId"].strip() for t in tracks):
        raise ValueError("YouTube returned an unidentified playlist item")
    count = detail.get("trackCount")
    if count is not None and (not isinstance(count, int) or count != len(tracks)):
        raise ValueError("YouTube returned an incomplete playlist")
    return detail
