"""Catalog lookups for alternate recordings and missing track durations."""
import logging
import re

from yt_playlist.util.duration import parse_duration
from yt_playlist.util.retry import with_retry
from yt_playlist.util.thumbnails import best_thumb

logger = logging.getLogger(__name__)


def fetch_song_duration(client, video_id):
    """Return a best-effort duration; unavailable metadata never blocks a playlist add."""
    if not video_id:
        return None
    try:
        details = (with_retry(lambda: client.get_song(video_id)) or {}).get("videoDetails") or {}
        seconds = details.get("lengthSeconds")
        return int(seconds) if seconds not in (None, "") else None
    except Exception:
        return None


_PARENS_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _strip_parens(text):
    return _PARENS_RE.sub("", text or "").strip()


def search_versions(client, title, artist, exclude=None, limit=14):
    """Search unfiltered results first, then backfill missing durations from songs and videos."""
    base_title = _strip_parens(title) or title
    query = " ".join(part for part in (base_title, artist) if part).strip()
    out, by_video = [], {}
    for kind in (None, "songs", "videos"):
        try:
            results = with_retry(lambda filter=kind: client.search(query, filter=filter)) or []
        except Exception:
            logger.warning("alternate-version search (%s) failed for %r", kind, query)
            results = []
        for result in results:
            video = result.get("videoId")
            if not video or video == exclude:
                continue
            duration = result.get("duration_seconds") or parse_duration(result.get("duration"))
            candidate = by_video.get(video)
            if candidate is not None:
                if candidate["duration"] is None and duration is not None:
                    candidate["duration"] = duration
                continue
            if len(out) >= limit:
                continue
            album = result.get("album")
            is_video = (kind == "videos" or result.get("resultType") == "video"
                        or result.get("videoType") == "MUSIC_VIDEO_TYPE_UGC")
            candidate = {
                "videoId": video, "title": result.get("title", ""),
                "artist": ", ".join(artist.get("name", "") for artist in (result.get("artists") or [])
                                    if artist.get("name")),
                "album": album.get("name") if isinstance(album, dict) else None,
                "album_browse": album.get("id") if isinstance(album, dict) else None,
                "duration": duration, "thumbnail": best_thumb(result.get("thumbnails")),
                "kind": "video" if is_video else "song"}
            by_video[video] = candidate
            out.append(candidate)
        if len(out) >= limit and all(candidate["duration"] is not None for candidate in out):
            break
    return out
