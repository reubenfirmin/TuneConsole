"""Last.fm enrichment: pin a clean genre from a track's (or artist's) top tags.

Last.fm has dense, crowd-sourced tags where MusicBrainz genres are sparse — but the tags are noisy
(moods, decades, "seen live"...). We match them against genres.py's whitelist and take the highest-
count tag that is a recognized genre. Last.fm doesn't give reliable release years, so this only sets
genre (MusicBrainz still owns year); it fills tracks that have no genre yet rather than overwriting.

Needs a (free) Last.fm API key: set $LASTFM_API_KEY, or `lastfm_api_key = "..."` in config.toml.
"""
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from yt_playlist.providers import genres
from yt_playlist.providers.base import EnrichmentResult, RateLimiter, run_enrich_loop
from yt_playlist.util import net
from yt_playlist.core import paths
from yt_playlist.providers.enrich_queue import PriorityGate

name = "lastfm"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

logger = logging.getLogger(__name__)

_API = "https://ws.audioscrobbler.com/2.0/"
_USER_AGENT = "yt-playlist/0.1 ( https://4rc.io ; rf@4rc.io )"
_MIN_INTERVAL = 0.25                      # Last.fm allows ~5 req/s; stay comfortably under
_HTTP_TIMEOUT_S = 20                      # cap each request so a stalled socket can't wedge a run
_pacer = RateLimiter(_MIN_INTERVAL)
_gate = PriorityGate()                    # newest enrichment job preempts older ones
_breaker = net.CircuitBreaker()           # stop a run once the host looks unreachable


class MissingKey(Exception):
    """Raised when no Last.fm API key is configured."""


def api_key(store=None, config_path=None):
    """Resolve the Last.fm API key: $LASTFM_API_KEY, then the value saved via the UI (settings
    table), then `lastfm_api_key` in config.toml. Returns None if none is set."""
    import os
    env = os.environ.get("LASTFM_API_KEY")
    if env and env.strip():
        return env.strip()
    if store is not None:
        saved = store.get_setting("lastfm_api_key")
        if saved and saved.strip():
            return saved.strip()
    path = config_path or paths.config_path()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None
    key = data.get("lastfm_api_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def available(store=None) -> bool:
    return api_key(store) is not None


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def probe(track, store=None, key=None) -> EnrichmentResult:
    """Read-only lookup: genre (and a year-ish tag/page year). Empty if no API key."""
    key = key or api_key(store)
    if not key:
        return EnrichmentResult("lastfm", {})
    genre, year = enrich(track["title"], track["artist"], key)
    fields = {}
    if genre:
        fields["genre"] = genre
    if year:
        fields["year"] = year
    return EnrichmentResult("lastfm", fields)


def _get(params):
    _pacer.wait()
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            data = json.load(resp)
    except Exception as e:                # report the outcome to the breaker, then let callers handle it
        _breaker.record(e)
        raise
    _breaker.record()
    return data


def _fetch_text(url):
    for attempt in (1, 2):                         # Last.fm pages 502 transiently — retry once
        _pacer.wait()
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _breaker.record(e)                     # server answered (an error) — host is reachable
            if e.code >= 500 and attempt == 1:
                time.sleep(1.0)
                continue
            raise
        except Exception as e:
            _breaker.record(e)
            raise
        _breaker.record()
        return text


def _page_release_year(html):
    """Pull the release year from a Last.fm track page's 'Release Date' metadata field."""
    m = re.search(r"Release Date\s*</dt>\s*<dd[^>]*>([^<]+)", html or "")
    if not m:
        return None
    ym = re.search(r"\b(1[89]\d\d|20\d\d)\b", m.group(1))
    return ym.group(1) if ym else None


def _tag_names(payload):
    """Tag names from a *.getTopTags response, highest-count first (the API already sorts them)."""
    tags = ((payload or {}).get("toptags") or {}).get("tag") or []
    if isinstance(tags, dict):                # the API returns a bare object for a single tag
        tags = [tags]
    return [t.get("name", "") for t in tags if t.get("name")]


def _year_from_tags(names):
    """Last.fm tags often include the release year (e.g. '1979'). Return the first plausible one."""
    for name in names:
        n = name.strip()
        if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099:
            return n
    return None


def artist_genre(artist, key):
    """Best whitelisted genre from an artist's Last.fm top tags, or None. For #18: tagging a new
    (unowned) discovered artist so the facet overlay can steer it. Best-effort — None on any failure."""
    if not key or not artist:
        return None
    common = {"api_key": key, "format": "json", "autocorrect": "1"}
    try:
        return genres.pick_genre(_tag_names(
            _get({"method": "artist.gettoptags", "artist": artist, **common})))
    except Exception as e:  # noqa: BLE001 - network/parse all degrade to no genre
        logger.warning("Last.fm artist top tags failed for %r: %s", artist, e)
        return None


def enrich(title, artist, key):
    """Return (genre, year) from Last.fm. One track.getInfo gives the tags (genre, via the whitelist)
    and the canonical page URL; the release year is read off that page's metadata (Last.fm exposes it
    as primary metadata, not in the API). Genre falls back to artist tags; year to a year-like tag."""
    common = {"api_key": key, "format": "json", "autocorrect": "1"}
    try:
        info = _get({"method": "track.getinfo", "artist": artist, "track": title, **common})
    except Exception as e:  # noqa: BLE001
        logger.warning("Last.fm getInfo failed for %r / %r: %s", title, artist, e)
        return (None, None)
    track = (info or {}).get("track") or {}
    track_tags = _tag_names(track)   # _tag_names guards the single-tag-as-dict case the API returns
    genre = genres.pick_genre(track_tags)
    if not genre and artist:                      # no track-level genre — fall back to artist tags
        try:
            genre = genres.pick_genre(_tag_names(
                _get({"method": "artist.gettoptags", "artist": artist, **common})))
        except Exception as e:  # noqa: BLE001
            logger.warning("Last.fm artist tags failed for %r: %s", artist, e)
    year = None
    album_url = (track.get("album") or {}).get("url")   # the ALBUM page carries the Release Date
    if album_url:
        try:
            year = _page_release_year(_fetch_text(album_url))
        except Exception as e:  # noqa: BLE001
            logger.warning("Last.fm album page fetch failed for %s: %s", album_url, e)
    if not year:
        year = _year_from_tags(track_tags)        # fall back to a year tag if the page had none
    return (genre, year)


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, key=None, should_stop=None, pending=None):
    """Fill missing genre and year for a track set from Last.fm (fill-only: never overwrites what's
    already there). Scope is a playlist (playlist_id) or an explicit `pending` list (an album's
    tracks). `on_progress` receives info/track/done events for the SSE stream."""
    enrich_fn = enrich_fn or enrich
    key = key or api_key(store)
    if not key:
        on_progress({"type": "err", "text": "No Last.fm API key. Set $LASTFM_API_KEY or "
                                            "lastfm_api_key in config.toml."})
        return
    pending = store.tracks_to_enrich(playlist_id) if pending is None else pending   # missing genre OR year

    def _per_item(i, total, t):
        genre, year = enrich_fn(t["title"], t["artist"], key)
        if _breaker.tripped():             # host unreachable — the rest would all fail too, so stop
            on_progress({"type": "err", "text": "Last.fm looks unreachable — stopped. "
                         "The remaining tracks will retry next time."})
            return False
        store.set_track_enrichment(t["id"], genre, year)
        eff_genre, eff_year = store.get_track_enrichment(t["id"])    # report what actually stuck
        bits = " · ".join(x for x in (genre, year) if x) or "no tags"
        on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                     "genre": eff_genre, "year": eff_year, "text": f"{i}/{total} {t['title']} — {bits}"})

    run_enrich_loop(
        store, on_progress, pending, gate=_gate, breaker=_breaker, should_stop=should_stop,
        empty_text="Every track already has genre & year.",
        start_text=lambda n: f"Tagging {n} track(s) via Last.fm…",
        done_text=lambda n: f"Tagged {n} track(s).",
        wait_text="Waiting — a newer playlist is tagging first…",
        per_item=_per_item)


def similar_artists(name, key, limit=50):
    """Last.fm artist.getSimilar -> [(artist_name, match_0_to_1)], most similar first. [] on error."""
    if not name or not key:
        return []
    try:
        data = _get({"method": "artist.getSimilar", "artist": name, "api_key": key,
                     "format": "json", "limit": limit, "autocorrect": 1})
    except (urllib.error.URLError, OSError, ValueError):
        return []
    out = []
    for a in (data.get("similarartists") or {}).get("artist") or []:
        nm = (a.get("name") or "").strip()
        try:
            match = float(a.get("match") or 0.0)
        except (TypeError, ValueError):
            match = 0.0
        if nm:
            out.append((nm, match))
    return out
