"""Keyless Wikipedia summaries for the 'into recently' Home card.

Resolve a page for an artist or genre (search, biased with a little context so 'Mercury' lands on
the band, not the planet), then fetch the REST summary. Only a real article (type 'standard' with a
non-empty extract) counts; disambiguation pages, empty extracts, and any network failure are a miss
(return None) so the card simply does not show rather than breaking the page.
"""
import json
import re
import urllib.parse
import urllib.request

from yt_playlist.providers.base import RateLimiter
from yt_playlist.util import net

name = "wikipedia"

# Words that carry no identity (added as search context, or generic): ignored when checking that a
# resolved page is actually about the subject.
_STOPWORDS = {"band", "musician", "music", "genre", "the", "and", "of", "a"}
_MAX_CANDIDATES = 5

_SEARCH = "https://en.wikipedia.org/w/api.php"
_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_USER_AGENT = "yt-playlist/0.1 ( https://4rc.io ; rf@4rc.io )"
_MIN_INTERVAL = 0.2                       # Wikipedia is generous; stay polite
_HTTP_TIMEOUT_S = 8                       # short: this runs on a Home fragment request
_pacer = RateLimiter(_MIN_INTERVAL)
_breaker = net.CircuitBreaker()

_CONTEXT = {"artist": "band musician", "genre": "music genre"}


def _get_json(url):
    """One HTTP GET returning parsed JSON. The single network seam (tests monkeypatch this)."""
    _pacer.wait()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            data = json.load(resp)
    except Exception as e:
        _breaker.record(e)
        raise
    _breaker.record()
    return data


def _tokens(text):
    """Identity-bearing lowercased words in `text` (drops short/stopword tokens like 'band')."""
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOPWORDS]


def _relates(subject_tokens, *texts):
    """True if any identity token of the subject appears in the candidate texts. Guards against a
    fuzzy search dropping us on an unrelated page (e.g. 'Martin Schulte' -> a Killing Joke album)."""
    if not subject_tokens:
        return True
    blob = " ".join(t.lower() for t in texts if t)
    return any(tok in blob for tok in subject_tokens)


def _search_titles(kind, display):
    term = f"{display} {_CONTEXT.get(kind, '')}".strip()
    params = {"action": "query", "format": "json", "list": "search",
              "srlimit": str(_MAX_CANDIDATES), "srsearch": term}
    data = _get_json(_SEARCH + "?" + urllib.parse.urlencode(params))
    hits = (((data or {}).get("query") or {}).get("search")) or []
    return [h.get("title") for h in hits if h.get("title")]


def _summary(title):
    url = _SUMMARY + urllib.parse.quote(title.replace(" ", "_")) + "?redirect=true"
    return _get_json(url)


def fetch_summary(kind, display):
    """Resolve and fetch a Wikipedia summary for an artist/genre, or None on any miss/failure.

    Walks the top search candidates and accepts the first that is a real article (type 'standard',
    non-empty extract) AND actually relates to the subject (an identity token of the name appears in
    the page title or extract). The relevance check stops a fuzzy search from returning a confidently
    wrong page for an obscure name."""
    want = _tokens(display)
    try:
        titles = _search_titles(kind, display)
    except Exception:                     # network / parse failure: treat as a miss, never raise
        return None
    for title in titles:
        if not _relates(want, title):     # cheap title prefilter: skip obvious mismatches
            continue
        try:
            s = _summary(title)
        except Exception:
            continue
        if not s or s.get("type") != "standard":
            continue
        extract = (s.get("extract") or "").strip()
        if not extract or not _relates(want, s.get("title"), extract):
            continue
        return {
            "display": display,
            "title": s.get("title") or title,
            "extract": extract,
            "thumbnail": ((s.get("thumbnail") or {}).get("source")),
            "url": (((s.get("content_urls") or {}).get("desktop") or {}).get("page")),
        }
    return None
