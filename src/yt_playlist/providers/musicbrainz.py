"""MusicBrainz enrichment: look up a track's genre and first-release year.

MusicBrainz asks clients to (a) send a descriptive User-Agent and (b) make at most ~1 request per
second. We honour both: a process-wide lock paces every call to >= 1s apart, and each track costs
two calls — a recording search (match + year from its releases) and a recording lookup (genre from
genres/tags). Network/parse failures degrade to (None, None) so one bad track never stops a run.
"""
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request

from yt_playlist.util import net
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.enrich_queue import PriorityGate

logger = logging.getLogger(__name__)

name = "musicbrainz"


def available(store=None) -> bool:
    return True


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def probe(track, store=None) -> EnrichmentResult:
    """Read-only lookup: genre, year, and the recording MBID (which keys AcousticBrainz)."""
    genre, year = enrich(track["title"], track["artist"])
    mbid = recording_mbid(track["title"], track["artist"])
    fields = {}
    if genre:
        fields["genre"] = genre
    if year:
        fields["year"] = year
    if mbid:
        fields["mb_recording_id"] = mbid
    return EnrichmentResult("musicbrainz", fields)

_API = "https://musicbrainz.org/ws/2"
# MusicBrainz blocks requests without a meaningful UA; identify the app + a contact.
_USER_AGENT = "yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )"
_MIN_INTERVAL = 1.1                       # seconds between requests (just over the 1/s limit)
_pace_lock = threading.Lock()
_last_call = [0.0]
_gate = PriorityGate()                    # newest enrichment job preempts older ones
_breaker = net.CircuitBreaker()           # stop a run once the host looks unreachable


def _get(path, params):
    with _pace_lock:                      # serialize + pace all MB traffic across threads
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    url = f"{_API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:                # report the outcome to the breaker, then let callers handle it
        _breaker.record(e)
        raise
    _breaker.record()
    return data


def _lucene_escape(text):
    # neutralize the Lucene query syntax in user-supplied title/artist
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in (text or ""))


def _years(dates):
    return [d[:4] for d in dates if d and len(d) >= 4 and d[:4].isdigit()]


def _earliest_year(recordings):
    """A song's original release year: the earliest recording-level first-release-date across ALL
    candidate recordings — the top search match is usually a remaster/compilation with a much later
    date. Fall back to release dates, then release-group dates."""
    frd = _years(r.get("first-release-date") for r in recordings)
    if frd:
        return min(frd)
    rel = _years(rel.get("date") for r in recordings for rel in (r.get("releases") or []))
    if rel:
        return min(rel)
    rg = _years(rel.get("release-group", {}).get("first-release-date")
                for r in recordings for rel in (r.get("releases") or []))
    return min(rg) if rg else None


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
        try:
            art = _get(f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"})
        except Exception as e:  # noqa: BLE001
            logger.warning("MB artist lookup failed for %s: %s", mbid, e)
            return None        # transient failure — don't cache, so a later track retries
        _artist_genre_cache[mbid] = _top_label(art)   # cache only on success (incl. a legit None)
    return _artist_genre_cache.get(mbid)


_PARENS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _strip_parens(title):
    """Drop parenthetical/bracketed qualifiers, e.g. 'No Quarter (Remaster)' -> 'No Quarter'."""
    return _PARENS.sub("", title or "").strip()


def recording_mbid(title, artist):
    """Best-match MusicBrainz recording MBID for a track, or None. Used to key AcousticBrainz.
    Retries once with parenthetical qualifiers stripped, same as enrich()."""
    mbid = _search_mbid(title, artist)
    if mbid is None:
        stripped = _strip_parens(title)
        if stripped and stripped != title:
            mbid = _search_mbid(stripped, artist)
    return mbid


def _search_mbid(title, artist):
    query = f'recording:"{_lucene_escape(title)}"'
    if artist:
        query += f' AND artist:"{_lucene_escape(artist)}"'
    try:
        res = _get("recording", {"query": query, "fmt": "json", "limit": "25"})
    except Exception as e:  # noqa: BLE001
        logger.warning("MB mbid search failed for %r / %r: %s", title, artist, e)
        return None
    recordings = res.get("recordings") or []
    return recordings[0].get("id") if recordings else None


def enrich(title, artist):
    """Return (genre, year) for a track, or (None, None) on no match / error.

    Genre falls back from recording -> artist level, because recording-level genres are sparse in
    MusicBrainz while artist-level ones are well populated. Year comes from the earliest release.
    If the full title finds nothing, retry once with parenthetical qualifiers stripped (a remaster /
    live / remix suffix often blocks the match).
    """
    result = _lookup(title, artist)
    if result == (None, None):
        stripped = _strip_parens(title)
        if stripped and stripped != title:
            result = _lookup(stripped, artist)
    return result


def _lookup(title, artist):
    query = f'recording:"{_lucene_escape(title)}"'
    if artist:
        query += f' AND artist:"{_lucene_escape(artist)}"'
    try:
        # wide net (25) so the original recording is in the pool, not just remasters near the top
        res = _get("recording", {"query": query, "fmt": "json", "limit": "25"})
    except Exception as e:  # noqa: BLE001
        logger.warning("MB search failed for %r / %r: %s", title, artist, e)
        return (None, None)
    recordings = res.get("recordings") or []
    if not recordings:
        return (None, None)
    rec = recordings[0]                          # best match drives genre/artist
    year = _earliest_year(recordings)            # but year comes from the earliest candidate
    credit = rec.get("artist-credit") or []
    first = credit[0] if credit else None        # MB sometimes returns bare join-phrase strings here
    artist_mbid = (first.get("artist") or {}).get("id") if isinstance(first, dict) else None
    genre = None
    mbid = rec.get("id")
    if mbid:
        try:
            full = _get(f"recording/{mbid}", {"inc": "genres+tags+releases", "fmt": "json"})
            genre = _top_label(full)
            year = year or _earliest_year([full])
        except Exception as e:  # noqa: BLE001
            logger.warning("MB lookup failed for %s: %s", mbid, e)
    if not genre:                              # recording had no genre/tag — use the artist's
        genre = _artist_genre(artist_mbid)
    return (genre, year)


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, should_stop=None, pending=None):
    """Walk a track set's not-yet-enriched tracks, fetch genre+year for each, persist, and report
    progress. Scope is a playlist (playlist_id) or an explicit `pending` list (e.g. an album's tracks).
    `on_progress(event_dict)` receives info/track/done events for the SSE stream."""
    enrich_fn = enrich_fn or enrich        # resolved here so tests can monkeypatch module-level enrich
    pending = store.tracks_to_enrich(playlist_id) if pending is None else pending
    total = len(pending)
    if not total:
        on_progress({"type": "done", "text": "Everything is already enriched.", "total": 0})
        return
    on_progress({"type": "info", "text": f"Enriching {total} track(s) via MusicBrainz…", "total": total})
    _breaker.reset()                       # fresh chance each run — a past outage shouldn't pre-trip it
    seq = _gate.enter()
    try:
        for i, t in enumerate(pending, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "text": "Waiting: a newer playlist is enriching first…"}))
            genre, year = enrich_fn(t["title"], t["artist"])
            if _breaker.tripped():         # host unreachable — the rest would all fail too, so stop
                on_progress({"type": "err", "text": "MusicBrainz looks unreachable; enrichment stopped. "
                             "The remaining tracks will retry next time."})
                return
            mbid = recording_mbid(t["title"], t["artist"])
            if mbid:
                store.set_track_mbid(t["id"], mbid)
            store.set_track_enrichment(t["id"], genre, year)
            eff_genre, eff_year = store.get_track_enrichment(t["id"])    # report what actually stuck
            bits = " · ".join(x for x in (genre, year) if x) or "no match"
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "genre": eff_genre, "year": eff_year,
                         "text": f"{i}/{total} {t['title']} — {bits}"})
        on_progress({"type": "done", "text": f"Enriched {total} track(s).", "total": total})
    finally:
        _gate.leave(seq)
