"""#91 Ingest raw player/curation events pushed by the extension ({type:'pevent'} bridge frames).

Everything lands append-only in player_events. Confirmed rating actions feed the like/dislike
model; exact choice-linked exits are also copied to immutable response evidence. Unseen uploads
retain their exact-video rating while the recommendation worker resolves metadata. Interpretation
(skip vs completion, sessions, curation signals) happens later, at read time, so thresholds stay
server-tunable without extension releases."""
import json
from urllib.parse import parse_qs, urlsplit


PLAYBACK_KINDS = {"track_exit", "ended", "state", "tick", "volume", "bye", "next"}
CURATION_KINDS = {"rate", "playlist_edit", "feedback", "subscription", "share_intent"}

# Server-written kinds: recorded directly via store.record_player_event by server-side routes, never
# pushed as a bridge frame, so they are intentionally absent from the two sets above and never seen
# by handle_player_event. "alt_version" (web/routes/playlists.py, the alternates-add route): the user
# swapped in an alternate version of a track, i.e. "these two are the same song to me" (future
# dedupe/preference evidence). Payload: {"old": <old video_id>, "new": <new video_id>}.

_BODY_CAP = 4096
# A successful request is authoritative, including an explicit unlike. Player-bar readouts
# remain untrusted for clearing a rating (see live_plays._STATUSES).
_RATE_STATUS = {"like": "LIKE", "dislike": "DISLIKE", "removelike": "INDIFFERENT"}
_PAYLOAD_EXTRAS = ("state", "volume", "muted", "tabMuted", "paused", "shuffle", "repeat")


def _extract_action_from_url(url):
    """Extract action from URL path, ignoring querystring (e.g., 'like', 'dislike' from .../{action}?...)."""
    try:
        path = urlsplit(url).path
        return path.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        return url.rstrip("/").rsplit("/", 1)[-1]


def _watch_playlist(href, video):
    if not video or not isinstance(href, str):
        return None
    try:
        watch = urlsplit(href)
    except ValueError:
        return None
    if (watch.scheme, watch.netloc, watch.path) != ('https', 'music.youtube.com', '/watch'):
        return None
    query = parse_qs(watch.query, keep_blank_values=True)
    playlists = query.get('list', [])
    if query.get('v') == [video] and len(playlists) == 1:
        return playlists[0] or None
    return None


def _curation_fields(kind, msg):
    """Best-effort (video_id, playlist_ytm_id, action) from an observed request. Unknown shapes
    yield Nones; the raw url/body/href payload is kept regardless, so nothing is lost."""
    url = msg.get("url") or ""
    try:
        data = json.loads(msg.get("body") or "")
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if kind == "rate":
        target = data.get("target")
        if not isinstance(target, dict):
            target = {}
        video = target.get("videoId")
        playlist = target.get("playlistId") or _watch_playlist(msg.get("href"), video)
        action = _extract_action_from_url(url)
        return video, playlist, action
    if kind == "playlist_edit":
        vids = [a.get("addedVideoId") or a.get("removedVideoId")
                for a in (data.get("actions") or []) if isinstance(a, dict)]
        vids = [v for v in vids if v]
        return (vids[0] if vids else None), data.get("playlistId"), None
    if kind == "subscription":
        return None, None, _extract_action_from_url(url)
    return None, None, None


