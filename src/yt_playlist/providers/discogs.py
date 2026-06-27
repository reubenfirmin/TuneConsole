"""Discogs enrichment: reliable genre and year from the Discogs release database.

Discogs search results carry structured `genre`, `style` and `year` fields. Styles are specific
(Techno, Trip Hop, …) and map cleanly onto our genre whitelist; year is the release year. We take
the earliest year across the top matches (avoids reissue dates) and the first style/genre that is a
recognized genre.

Works anonymously (25 requests/min) or, with a free personal access token, faster (60/min). Set a
token via $DISCOGS_TOKEN or `discogs_token` in config.toml / the settings table. It's optional.
"""
import json
import logging
import sys
import urllib.parse
import urllib.request

from yt_playlist.providers import genres
from yt_playlist.providers.base import EnrichmentResult, RateLimiter, run_enrich_loop
from yt_playlist.util import net
from yt_playlist.core import paths
from yt_playlist.providers.enrich_queue import PriorityGate

name = "discogs"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

logger = logging.getLogger(__name__)

_API = "https://api.discogs.com/database/search"
_USER_AGENT = "yt-playlist/0.1 +https://4rc.io"
_HTTP_TIMEOUT_S = 20                       # cap each request so a stalled socket can't wedge a run
_pacer = RateLimiter(2.5)                  # interval is set per-call in _pace() (token-dependent)
_gate = PriorityGate()                    # newest enrichment job preempts older ones
_breaker = net.CircuitBreaker()           # stop a run once the host looks unreachable


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


def available(store=None) -> bool:
    return True


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def probe(track, store=None, tok=None) -> EnrichmentResult:
    """Read-only lookup: genre & year from Discogs releases."""
    tok = tok or token(store)
    genre, year = enrich(track["title"], track["artist"], tok)
    fields = {}
    if genre:
        fields["genre"] = genre
    if year:
        fields["year"] = year
    return EnrichmentResult("discogs", fields)


def _pace(tok):
    interval = 1.1 if tok else 2.5            # 60/min authenticated, 25/min anonymous
    _pacer.wait(interval)


def _search(query, tok):
    params = {"q": query, "type": "release", "per_page": "5"}
    if tok:
        params["token"] = tok
    _pace(tok)
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            data = json.load(resp)
    except Exception as e:                # report the outcome to the breaker, then let callers handle it
        _breaker.record(e)
        raise
    _breaker.record()
    return (data or {}).get("results") or []


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
    year = str(min(years)) if years else None  # earliest release: avoids reissue dates
    return (genre, year)


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, tok=None, should_stop=None, pending=None):
    """Fill missing genre and year for a track set from Discogs (fill-only). Scope is a playlist
    (playlist_id) or an explicit `pending` list (an album's tracks)."""
    enrich_fn = enrich_fn or enrich
    tok = tok or token(store)
    pending = store.tracks_to_enrich(playlist_id) if pending is None else pending   # missing genre OR year
    auth = "with token" if tok else "anonymously (slower)"

    def _per_item(i, total, t):
        genre, year = enrich_fn(t["title"], t["artist"], tok)
        if _breaker.tripped():             # host unreachable. The rest would all fail too, so stop
            on_progress({"type": "err", "text": "Discogs looks unreachable. Stopped. "
                         "The remaining tracks will retry next time."})
            return False
        store.set_track_enrichment(t["id"], genre, year)
        eff_genre, eff_year = store.get_track_enrichment(t["id"])
        bits = " · ".join(x for x in (genre, year) if x) or "no match"
        on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                     "genre": eff_genre, "year": eff_year, "text": f"{i}/{total} {t['title']}: {bits}"})

    run_enrich_loop(
        store, on_progress, pending, gate=_gate, breaker=_breaker, should_stop=should_stop,
        empty_text="Every track already has genre & year.",
        start_text=lambda n: f"Looking up {n} track(s) on Discogs {auth}…",
        done_text=lambda n: f"Looked up {n} track(s).",
        wait_text="Waiting: a newer playlist is looking up first…",
        per_item=_per_item)
