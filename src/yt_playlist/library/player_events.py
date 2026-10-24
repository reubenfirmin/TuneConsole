"""#91 Ingest raw player/curation events pushed by the extension ({type:'pevent'} bridge frames).

Everything lands append-only in player_events. Confirmed rating actions feed the like/dislike
model; exact choice-linked exits are also copied to immutable response evidence. Unseen uploads
retain their exact-video rating while the recommendation worker resolves metadata. Interpretation
(skip vs completion, sessions, curation signals) happens later, at read time, so thresholds stay
server-tunable without extension releases."""
import json
from urllib.parse import urlsplit

from yt_playlist.library.live_plays import resolve_identity
from yt_playlist.library import ratings
from yt_playlist.util.curation import confirmed_rate

PLAYBACK_KINDS = {"track_exit", "ended", "state", "tick", "volume", "bye"}
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
_PAYLOAD_EXTRAS = ("state", "volume", "shuffle", "repeat")


def _extract_action_from_url(url):
    """Extract action from URL path, ignoring querystring (e.g., 'like', 'dislike' from .../{action}?...)."""
    try:
        path = urlsplit(url).path
        return path.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        return url.rstrip("/").rsplit("/", 1)[-1]


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
        target = data.get("target") or {}
        action = _extract_action_from_url(url)
        return target.get("videoId"), target.get("playlistId"), action
    if kind == "playlist_edit":
        vids = [a.get("addedVideoId") or a.get("removedVideoId")
                for a in (data.get("actions") or []) if isinstance(a, dict)]
        vids = [v for v in vids if v]
        return (vids[0] if vids else None), data.get("playlistId"), None
    if kind == "subscription":
        return None, None, _extract_action_from_url(url)
    return None, None, None


def handle_player_event(ctx, msg, now) -> bool:
    """Persist one pevent frame. Returns True when a row was recorded."""
    kind = (msg.get("kind") or "").strip()
    if kind not in PLAYBACK_KINDS and kind not in CURATION_KINDS:
        return False
    store = ctx.store
    ident = resolve_identity(store, (msg.get("brandId") or "").strip())
    if ident is None:
        return False
    if kind in PLAYBACK_KINDS:
        video_id = msg.get("videoId") or None
        playlist = msg.get("playlist") or None
        position, duration, action = msg.get("position"), msg.get("duration"), None
        payload = {k: msg[k] for k in _PAYLOAD_EXTRAS if msg.get(k) not in (None, "")}
    else:
        video_id, playlist, action = _curation_fields(kind, msg)
        position = duration = None
        payload = {"url": msg.get("url"), "body": (msg.get("body") or "")[:_BODY_CAP],
                   "href": msg.get("href")}
        if action:
            payload["action"] = action
        payload.update({k: msg[k] for k in ("phase", "success", "httpStatus", "error") if k in msg})
    event_id = store.player_events.record_player_event(ident, kind, video_id, position, duration, playlist,
                              json.dumps(payload) if payload else None, now)
    if kind in {'track_exit', 'ended', 'bye'}:
        from yt_playlist.rec import choice_outcomes
        choice_outcomes.observe(store, ident, playlist, video_id, kind, position, duration, event_id, now)
    if kind == "rate" and confirmed_rate(msg) and video_id and action in _RATE_STATUS:
        ratings.record_confirmed(store, ident, video_id, _RATE_STATUS[action], now)
        worker = getattr(ctx, "rec_worker", None)
        if worker is not None:
            worker.trigger_rating_update()
    return True
