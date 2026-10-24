"""Last.fm enrichment: pin a clean genre from a track's (or artist's) top tags.

Last.fm has dense, crowd-sourced tags where MusicBrainz genres are sparse, but the tags are noisy
(moods, decades, "seen live"...). We match them against genres.py's whitelist and take the highest-
count tag that is a recognized genre. Last.fm doesn't give reliable release years, so this only sets
genre (MusicBrainz still owns year); it fills tracks that have no genre yet rather than overwriting.

Needs a (free) Last.fm API key: set $LASTFM_API_KEY, or `lastfm_api_key = "..."` in config.toml.
"""
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from yt_playlist.providers import credentials, genres, transport
from yt_playlist.util.matching import is_remix
from yt_playlist.providers.base import EnrichmentResult, RateLimiter
from yt_playlist.util import net

name = "lastfm"

logger = logging.getLogger(__name__)

_API = "https://ws.audioscrobbler.com/2.0/"
_USER_AGENT = "yt-playlist/0.1 ( https://4rc.io ; rf@4rc.io )"
_MIN_INTERVAL = 0.25                      # Last.fm allows ~5 req/s; stay comfortably under
_HTTP_TIMEOUT_S = 20                      # cap each request so a stalled socket can't wedge a run
_pacer = RateLimiter(_MIN_INTERVAL)
_breaker = net.CircuitBreaker()           # stop a run once the host looks unreachable


class MissingKey(Exception):
    """Raised when no Last.fm API key is configured."""


def api_key(store=None, config_path=None):
    """Resolve the Last.fm API key: $LASTFM_API_KEY, then the value saved via the UI (settings
    table), then `lastfm_api_key` in config.toml. Returns None if none is set."""
    return credentials.resolve(env_name="LASTFM_API_KEY", setting_name="lastfm_api_key",
                               store=store, config_path=config_path)


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


def _get(params, *, timeout=_HTTP_TIMEOUT_S):
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    data = transport.request_json(req, timeout=timeout, pace=_pacer.wait, breaker=_breaker)
    if isinstance(data, dict) and data.get("error") and data["error"] != 6:
        # Last.fm reports invalid credentials, rate limits and service failures inside HTTP 200.
        # Error 6 is a missing artist/track, which is a valid no-match answer.
        net.record_probe_failure("Last.fm API error")
    return data


def _fetch_text(url):
    """Fetch a Last.fm WEBSITE page (not the API). Retries once on a connection-level failure, which
    is genuinely transient - but not on an HTTP error: the server answered, and asking it the same
    question a second later has been observed to return the same 502, at double the cost."""
    for attempt in (1, 2):
        _pacer.wait()
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _breaker.record(e)                     # server answered (an error). Host is reachable
            raise
        except Exception as e:
            _breaker.record(e)
            if attempt == 1:
                time.sleep(1.0)
                continue
            raise
        _breaker.record()
        return text


# The album-page scrape is the only part of this provider that reads the WEBSITE instead of the API,
# and Last.fm 502s those pages in bursts - measured at 6 of 8 well-known albums, with a browser
# User-Agent failing identically, so it is their side and not something we can dress our way around.
# Once it is clearly not working, stand down for a while rather than spending a request per track to
# rediscover that. Nothing else is affected: the API keeps working, and the year has other providers.
_PAGE_FAIL_LIMIT = 5
_PAGE_COOLDOWN_S = 900
_page_fails = 0
_page_blocked_until = 0.0


def _page_scrape_ready():
    return time.monotonic() >= _page_blocked_until


def _record_page_result(ok):
    global _page_fails, _page_blocked_until
    if ok:
        _page_fails = 0
        return
    _page_fails += 1
    if _page_fails >= _PAGE_FAIL_LIMIT:
        _page_fails = 0
        _page_blocked_until = time.monotonic() + _PAGE_COOLDOWN_S
        logger.info("Last.fm album pages keep failing; skipping the release-year scrape for %d min. "
                    "Genre (API) is unaffected, and MusicBrainz/Discogs/Deezer still supply the year.",
                    _PAGE_COOLDOWN_S // 60)


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
    (unowned) discovered artist so the facet overlay can steer it. Best-effort: None on any failure."""
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
    if not genre and artist and not is_remix(title):
        try:
            genre = genres.pick_genre(_tag_names(
                _get({"method": "artist.gettoptags", "artist": artist, **common})))
        except Exception as e:  # noqa: BLE001
            logger.warning("Last.fm artist tags failed for %r: %s", artist, e)
    # Cheapest source first: the tags are already in hand, so a year tag costs nothing. Only when
    # they don't carry one is the album page worth a request (it holds the real Release Date).
    year = _year_from_tags(track_tags)
    album_url = (track.get("album") or {}).get("url")
    if not year and album_url and _page_scrape_ready():
        try:
            year = _page_release_year(_fetch_text(album_url))
            _record_page_result(True)
        except Exception as e:  # noqa: BLE001 - a missing year is a miss, not a failure
            _record_page_result(False)
            logger.debug("Last.fm album page unavailable for %s: %s", album_url, e)
    return (genre, year)


def tag_top_artists(tag, key, limit=25):
    """Last.fm tag.getTopArtists -> [artist_name], most-listened first. [] on error/no key.

    The ranking is what makes this useful: "who are the big alternative rock artists" is a question
    Last.fm's own listening data answers well, and far better than a text search for the genre name.
    """
    if not tag or not key:
        return []
    try:
        data = _get({"method": "tag.getTopArtists", "tag": tag, "api_key": key,
                     "format": "json", "limit": limit})
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("Last.fm tag.getTopArtists failed for %r: %s", tag, e)
        return []
    out = []
    for a in (data.get("topartists") or {}).get("artist") or []:
        nm = (a.get("name") or "").strip()
        if nm and nm not in out:
            out.append(nm)
    return out


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
