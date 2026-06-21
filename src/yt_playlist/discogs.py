"""Discogs enrichment: reliable genre and year from the Discogs release database.

Discogs search results carry structured `genre`, `style` and `year` fields. Styles are specific
(Techno, Trip Hop, …) and map cleanly onto our genre whitelist; year is the release year. We take
the earliest year across the top matches (avoids reissue dates) and the first style/genre that is a
recognized genre.

Works anonymously (25 requests/min) or, with a free personal access token, faster (60/min). Set a
token via $DISCOGS_TOKEN or `discogs_token` in config.toml / the settings table — it's optional.
"""
import json
import logging
import sys
import threading
import time
import urllib.parse
import urllib.request

from yt_playlist import genres, paths
from yt_playlist.enrich_queue import PriorityGate

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

logger = logging.getLogger(__name__)

_API = "https://api.discogs.com/database/search"
_USER_AGENT = "yt-playlist/0.1 +https://4rc.io"
_pace_lock = threading.Lock()
_last_call = [0.0]
_gate = PriorityGate()                    # newest enrichment job preempts older ones


def token(store=None, config_path=None):
    """Optional Discogs token: $DISCOGS_TOKEN, then the settings table, then config.toml."""
    import os
    env = os.environ.get("DISCOGS_TOKEN")
    if env and env.strip():
        return env.strip()
    if store is not None:
        saved = store.get_setting("discogs_token")
        if saved and saved.strip():
            return saved.strip()
    path = config_path or paths.config_path()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None
    tok = data.get("discogs_token")
    return tok.strip() if isinstance(tok, str) and tok.strip() else None


def _pace(tok):
    interval = 1.1 if tok else 2.5            # 60/min authenticated, 25/min anonymous
    with _pace_lock:
        wait = interval - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _search(query, tok):
    params = {"q": query, "type": "release", "per_page": "5"}
    if tok:
        params["token"] = tok
    _pace(tok)
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return (json.load(resp) or {}).get("results") or []


def enrich(title, artist, tok=None):
    """Return (genre, year) from Discogs, or (None, None). Either may be None."""
    query = " ".join(x for x in (artist, title) if x).strip()
    try:
        results = _search(query, tok)
    except Exception as e:  # noqa: BLE001
        logger.warning("Discogs search failed for %r / %r: %s", title, artist, e)
        return (None, None)
    genre = None
    for r in results:                          # first match whose style/genre is a known genre
        genre = genres.pick_genre((r.get("style") or []) + (r.get("genre") or []))
        if genre:
            break
    years = [int(str(r.get("year"))) for r in results
             if str(r.get("year") or "").isdigit() and 1900 <= int(r["year"]) <= 2099]
    year = str(min(years)) if years else None  # earliest release — avoids reissue dates
    return (genre, year)


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, tok=None, should_stop=None):
    """Fill missing genre and year for a playlist's tracks from Discogs (fill-only)."""
    enrich_fn = enrich_fn or enrich
    tok = tok or token(store)
    pending = store.tracks_to_enrich(playlist_id)        # missing genre OR year
    total = len(pending)
    if not total:
        on_progress({"type": "done", "text": "Every track already has genre & year.", "total": 0})
        return
    auth = "with token" if tok else "anonymously (slower)"
    on_progress({"type": "info", "text": f"Looking up {total} track(s) on Discogs {auth}…", "total": total})
    seq = _gate.enter()
    try:
        for i, t in enumerate(pending, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "text": "Waiting — a newer playlist is looking up first…"}))
            genre, year = enrich_fn(t["title"], t["artist"], tok)
            store.set_track_enrichment(t["id"], genre, year)
            eff_genre, eff_year = store.get_track_enrichment(t["id"])
            bits = " · ".join(x for x in (genre, year) if x) or "no match"
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "genre": eff_genre, "year": eff_year, "text": f"{i}/{total} {t['title']} — {bits}"})
        on_progress({"type": "done", "text": f"Looked up {total} track(s).", "total": total})
    finally:
        _gate.leave(seq)
