"""MusicBrainz enrichment: look up a track's genre and first-release year.

MusicBrainz asks clients to (a) send a descriptive User-Agent and (b) make at most ~1 request per
second. We honour both: a process-wide lock paces every call to >= 1s apart, and each track costs
two calls — a recording search (match + year from its releases) and a recording lookup (genre from
genres/tags). Network/parse failures degrade to (None, None) so one bad track never stops a run.
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_API = "https://musicbrainz.org/ws/2"
# MusicBrainz blocks requests without a meaningful UA; identify the app + a contact.
_USER_AGENT = "yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )"
_MIN_INTERVAL = 1.1                       # seconds between requests (just over the 1/s limit)
_pace_lock = threading.Lock()
_last_call = [0.0]


def _get(path, params):
    with _pace_lock:                      # serialize + pace all MB traffic across threads
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    url = f"{_API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _lucene_escape(text):
    # neutralize the Lucene query syntax in user-supplied title/artist
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in (text or ""))


def _earliest_year(*objs):
    dates = []
    for obj in objs:
        if not obj:
            continue
        if obj.get("first-release-date"):
            dates.append(obj["first-release-date"])
        for rel in obj.get("releases", []):
            if rel.get("date"):
                dates.append(rel["date"])
            rg = rel.get("release-group") or {}
            if rg.get("first-release-date"):
                dates.append(rg["first-release-date"])
    years = [d[:4] for d in dates if len(d) >= 4 and d[:4].isdigit()]
    return min(years) if years else None


def _top_label(obj):
    """Highest-voted genre, else highest-voted tag, title-cased for display."""
    for field in ("genres", "tags"):
        items = obj.get(field) or []
        if items:
            best = max(items, key=lambda x: x.get("count", 0)).get("name")
            if best:
                return best.title()
    return None


_artist_genre_cache = {}


def _artist_genre(mbid):
    """Artist-level genre (MusicBrainz keeps most genre data on artists, not recordings).
    Cached per process so a same-artist playlist only costs one lookup."""
    if not mbid:
        return None
    if mbid not in _artist_genre_cache:
        genre = None
        try:
            art = _get(f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"})
            genre = _top_label(art)
        except Exception as e:  # noqa: BLE001
            logger.warning("MB artist lookup failed for %s: %s", mbid, e)
        _artist_genre_cache[mbid] = genre
    return _artist_genre_cache[mbid]


def enrich(title, artist):
    """Return (genre, year) for a track, or (None, None) on no match / error.

    Genre falls back from recording -> artist level, because recording-level genres are sparse in
    MusicBrainz while artist-level ones are well populated. Year comes from the earliest release.
    """
    query = f'recording:"{_lucene_escape(title)}"'
    if artist:
        query += f' AND artist:"{_lucene_escape(artist)}"'
    try:
        res = _get("recording", {"query": query, "fmt": "json", "limit": "3"})
    except Exception as e:  # noqa: BLE001
        logger.warning("MB search failed for %r / %r: %s", title, artist, e)
        return (None, None)
    recordings = res.get("recordings") or []
    if not recordings:
        return (None, None)
    rec = recordings[0]
    year = _earliest_year(rec)
    credit = rec.get("artist-credit") or []
    artist_mbid = (credit[0].get("artist") or {}).get("id") if credit else None
    genre = None
    mbid = rec.get("id")
    if mbid:
        try:
            full = _get(f"recording/{mbid}", {"inc": "genres+tags+releases", "fmt": "json"})
            genre = _top_label(full)
            year = year or _earliest_year(full)
        except Exception as e:  # noqa: BLE001
            logger.warning("MB lookup failed for %s: %s", mbid, e)
    if not genre:                              # recording had no genre/tag — use the artist's
        genre = _artist_genre(artist_mbid)
    return (genre, year)


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, should_stop=None):
    """Walk a playlist's not-yet-enriched tracks, fetch genre+year for each, persist, and report
    progress. `on_progress(event_dict)` receives info/track/done events for the SSE stream."""
    enrich_fn = enrich_fn or enrich        # resolved here so tests can monkeypatch module-level enrich
    pending = store.tracks_to_enrich(playlist_id)
    total = len(pending)
    if not total:
        on_progress({"type": "done", "text": "Everything is already enriched.", "total": 0})
        return
    on_progress({"type": "info", "text": f"Enriching {total} track(s) via MusicBrainz…", "total": total})
    for i, t in enumerate(pending, 1):
        if should_stop and should_stop():
            on_progress({"type": "info", "text": "Stopped."})
            return
        genre, year = enrich_fn(t["title"], t["artist"])
        store.set_track_enrichment(t["id"], genre, year)
        bits = " · ".join(x for x in (genre, year) if x) or "no match"
        on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                     "genre": genre or "", "year": year or "",
                     "text": f"{i}/{total} {t['title']} — {bits}"})
    on_progress({"type": "done", "text": f"Enriched {total} track(s).", "total": total})
