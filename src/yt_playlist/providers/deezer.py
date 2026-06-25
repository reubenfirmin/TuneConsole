"""Deezer enrichment: tempo (BPM) for a track, free and without authentication.

Deezer's public API needs no API key for catalog reads. We resolve a track in two calls — an
advanced track search (artist + title) to get its id, then a track lookup whose object carries the
`bpm` field (search/listing responses omit it). Deezer reports bpm=0 for tracks it hasn't analysed,
which we treat as unknown. Deezer also signals quota/errors via an {"error": ...} body on an HTTP
200, so we check for that explicitly. Failures degrade to None so one bad track never stops a run.
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

from yt_playlist.util import net
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.enrich_queue import PriorityGate

logger = logging.getLogger(__name__)

name = "deezer"


def available(store=None) -> bool:
    return True


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def probe(track, store=None) -> EnrichmentResult:
    """Read-only lookup: bpm, popularity, gain, label."""
    feat = enrich(track["title"], track["artist"])     # {bpm, popularity, gain, label}
    fields = {k: v for k, v in feat.items() if v is not None}
    return EnrichmentResult("deezer", fields)

_API = "https://api.deezer.com"
_USER_AGENT = "yt-playlist/0.1 +https://4rc.io"
_MIN_INTERVAL = 0.15                       # ~50 req / 5s soft limit -> stay well under
_pace_lock = threading.Lock()
_last_call = [0.0]
_gate = PriorityGate()
_breaker = net.CircuitBreaker()


def _get_json(url):
    with _pace_lock:                       # serialize + pace all Deezer traffic across threads
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:                 # report to the breaker, then let the caller decide
        _breaker.record(e)
        raise
    _breaker.record()
    return data


_FIELDS = ("bpm", "popularity", "gain", "label")


def _empty():
    return {k: None for k in _FIELDS}


def enrich(title, artist):
    """Return a dict {bpm, popularity, gain, label} for a track; each value float/int/str or None.
    All-None on no match / unknown / error."""
    feat = _empty()
    q = " ".join(p for p in (f'artist:"{artist}"' if artist else "",
                             f'track:"{title}"' if title else "") if p)
    search_url = _API + "/search/track?" + urllib.parse.urlencode({"q": q, "limit": "1"})
    try:
        res = _get_json(search_url)
    except Exception as e:  # noqa: BLE001
        logger.warning("Deezer search failed for %r / %r: %s", title, artist, e)
        return feat
    if isinstance(res, dict) and res.get("error"):
        logger.warning("Deezer error for %r / %r: %s", title, artist, res["error"])
        return feat
    data = (res or {}).get("data") or []
    if not data:
        return feat
    track_id = data[0].get("id")
    if track_id is None:
        return feat
    try:
        track = _get_json(_API + f"/track/{track_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning("Deezer track lookup failed for %s: %s", track_id, e)
        return feat
    if isinstance(track, dict) and track.get("error"):
        return feat
    try:
        bpm = float(track.get("bpm"))
        feat["bpm"] = bpm if bpm > 0 else None     # Deezer reports 0 for un-analysed tracks
    except (TypeError, ValueError):
        feat["bpm"] = None
    rank = track.get("rank")
    feat["popularity"] = int(rank) if isinstance(rank, (int, float)) else None
    try:
        gain = track.get("gain")
        feat["gain"] = float(gain) if gain is not None else None
    except (TypeError, ValueError):
        feat["gain"] = None
    album_id = (track.get("album") or {}).get("id")
    if album_id is not None:
        try:
            album = _get_json(_API + f"/album/{album_id}")
            if isinstance(album, dict) and not album.get("error"):
                feat["label"] = album.get("label") or None
        except Exception as e:  # noqa: BLE001
            logger.info("Deezer album lookup failed for %s: %s", album_id, e)
    return feat


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, should_stop=None, pending=None):
    """Fill missing BPM for a track set from Deezer (fill-only). Scope is a playlist or an explicit
    `pending` list. on_progress(event_dict) feeds the SSE stream."""
    enrich_fn = enrich_fn or enrich
    pending = store.tracks_missing_audio(playlist_id) if pending is None else pending
    total = len(pending)
    if not total:
        on_progress({"type": "done", "text": "Every track already has audio features.", "total": 0})
        return
    on_progress({"type": "info", "text": f"Looking up BPM for {total} track(s) on Deezer…",
                 "total": total})
    _breaker.reset()
    seq = _gate.enter()
    try:
        for i, t in enumerate(pending, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "text": "Waiting — a newer playlist is looking up first…"}))
            feat = enrich_fn(t["title"], t["artist"])
            if _breaker.tripped():
                on_progress({"type": "err", "text": "Deezer looks unreachable — stopped. "
                             "The remaining tracks will retry next time."})
                return
            store.set_track_audio(t["id"], **feat)
            shown = f"{feat['bpm']:.0f} BPM" if feat["bpm"] else "no BPM"
            if feat["popularity"]:
                shown += f" · pop {feat['popularity']}"
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "text": f"{i}/{total} {t['title']} — {shown}"})
        on_progress({"type": "done", "text": f"Looked up {total} track(s).", "total": total})
    finally:
        _gate.leave(seq)
