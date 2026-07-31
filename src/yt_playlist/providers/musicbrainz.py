"""MusicBrainz enrichment: look up a track's genre and first-release year.

MusicBrainz asks clients to (a) send a descriptive User-Agent and (b) make at most ~1 request per
second. We honour both: a process-wide lock paces every call to >= 1s apart, and each track costs
two calls: a recording search (match + year from its releases) and a recording lookup (genre from
genres/tags). Network/parse failures degrade to (None, None) so one bad track never stops a run.
"""
import json
import logging
import re
import urllib.parse
import urllib.request

from yt_playlist.util import net
from yt_playlist.providers.base import EnrichmentResult, RateLimiter, run_enrich_loop
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
    """Read-only lookup: genre, year, the recording MBID (which keys AcousticBrainz), and the
    candidate release MBIDs (which key Cover Art Archive). All from the one search."""
    genre, year, mbid, release_ids = enrich_full(track["title"], track["artist"])
    fields = {}
    if genre:
        fields["genre"] = genre
    if year:
        fields["year"] = year
    if mbid:
        fields["mb_recording_id"] = mbid
    if release_ids:
        fields["mb_release_ids"] = release_ids
    return EnrichmentResult("musicbrainz", fields)

_API = "https://musicbrainz.org/ws/2"
# MusicBrainz blocks requests without a meaningful UA; identify the app + a contact.
_USER_AGENT = "yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )"
_MIN_INTERVAL = 1.1                       # seconds between requests (just over the 1/s limit)
_HTTP_TIMEOUT_S = 20                      # cap each request so a stalled socket can't wedge a run
_pacer = RateLimiter(_MIN_INTERVAL)
_gate = PriorityGate()                    # newest enrichment job preempts older ones
_breaker = net.CircuitBreaker()           # stop a run once the host looks unreachable


def _get(path, params):
    _pacer.wait()                         # serialize + pace all MB traffic across threads
    url = f"{_API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            data = json.load(resp)
    except Exception as e:                # report the outcome to the breaker, then let callers handle it
        _breaker.record(e)
        raise
    _breaker.record()
    return data


def _lucene_escape(text):
    # neutralize the Lucene query syntax in user-supplied title/artist
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in (text or ""))


def tag_artists(tag, decade=None, limit=25):
    """Artists MusicBrainz files under a genre tag, optionally narrowed to those who RELEASED in a
    given decade. [] on error.

    Queries release-groups rather than artists when a decade is given: an artist's own date range is
    when they existed, not when they put anything out, and "80s new wave" means records from the 80s.
    MB has no popularity ranking, so this is a source of NAMES - the ordering that matters comes from
    whoever consumes them (Last.fm's listener counts, then YouTube's own per-artist track ranking)."""
    tag = (tag or "").strip()
    if not tag:
        return []
    query = f'tag:"{_lucene_escape(tag)}"'
    try:
        if decade:
            query += f" AND firstreleasedate:[{int(decade)} TO {int(decade) + 9}]"
            res = _get("release-group", {"query": query, "fmt": "json", "limit": str(limit)})
            rows = res.get("release-groups") or []
            names = [((r.get("artist-credit") or [{}])[0].get("name") or "").strip() for r in rows]
        else:
            res = _get("artist", {"query": query, "fmt": "json", "limit": str(limit)})
            names = [(a.get("name") or "").strip() for a in (res.get("artists") or [])]
    except Exception as e:  # noqa: BLE001 - network/parse all degrade to "no artists found"
        logger.warning("MusicBrainz tag artists failed for %r: %s", tag, e)
        return []
    out = []
    for nm in names:
        if nm and nm not in out:
            out.append(nm)
    return out


def _years(dates):
    return [d[:4] for d in dates if d and len(d) >= 4 and d[:4].isdigit()]


def _earliest_year(recordings):
    """A song's original release year: the earliest recording-level first-release-date across ALL
    candidate recordings. The top search match is usually a remaster/compilation with a much later
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
            return None        # transient failure: don't cache, so a later track retries
        _artist_genre_cache[mbid] = _top_label(art)   # cache only on success (incl. a legit None)
    return _artist_genre_cache.get(mbid)


_PARENS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _strip_parens(title):
    """Drop parenthetical/bracketed qualifiers, e.g. 'No Quarter (Remaster)' -> 'No Quarter'."""
    return _PARENS.sub("", title or "").strip()


def recording_mbid(title, artist):
    """Best-match MusicBrainz recording MBID for a track, or None. Used to key AcousticBrainz.
    Retries once with parenthetical qualifiers stripped, same as enrich()."""
    recs = _recording_search(title, artist)
    return recs[0].get("id") if recs else None


def _search_recordings(title, artist):
    """Wide-net MB recording search (limit 25 so the original is in the pool, not just remasters
    near the top). Returns the recordings list, best match first, or [] on no match / error."""
    query = f'recording:"{_lucene_escape(title)}"'
    if artist:
        query += f' AND artist:"{_lucene_escape(artist)}"'
    try:
        res = _get("recording", {"query": query, "fmt": "json", "limit": "25"})
    except Exception as e:  # noqa: BLE001
        logger.warning("MB search failed for %r / %r: %s", title, artist, e)
        return []
    return res.get("recordings") or []


def _recording_search(title, artist):
    """One recording search with the parens-stripped retry (a remaster / live / remix suffix often
    blocks the match). Shared by enrich(), recording_mbid() and enrich_full() so a single track
    costs one search, not one per caller."""
    recs = _search_recordings(title, artist)
    if not recs:
        stripped = _strip_parens(title)
        if stripped and stripped != title:
            recs = _search_recordings(stripped, artist)
    return recs


def enrich(title, artist):
    """Return (genre, year) for a track, or (None, None) on no match / error.

    Genre falls back from recording -> artist level, because recording-level genres are sparse in
    MusicBrainz while artist-level ones are well populated. Year comes from the earliest release.
    """
    return _genre_year(_recording_search(title, artist))


def _release_ids(recordings):
    """Candidate release MBIDs, best match first. Cover Art Archive is keyed by RELEASE (not
    recording), and these ride along in the search response we already made, so the fallback costs
    no extra MusicBrainz traffic. De-duplicated: the same release recurs across candidates."""
    ids = []
    for rec in recordings:
        for rel in (rec.get("releases") or []):
            rid = rel.get("id")
            if rid and rid not in ids:
                ids.append(rid)
    return ids


def enrich_full(title, artist):
    """(genre, year, mb_recording_id, release_ids) from a SINGLE recording search. Callers that also
    want the MBID (the enrichment waterfall, the playlist runner) use this instead of enrich() plus a
    separate recording_mbid(), so a track costs one search rather than two.

    release_ids keys Cover Art Archive (which is keyed by release, not recording). It rides along
    here rather than in its own function so the cover fallback costs no extra search, and so this
    stays the single seam where MusicBrainz touches the network."""
    recs = _recording_search(title, artist)
    genre, year = _genre_year(recs)
    return genre, year, (recs[0].get("id") if recs else None), _release_ids(recs)


def _genre_year(recordings):
    """Derive (genre, year) from a recordings list (best match first), or (None, None) if empty."""
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
    if not genre:                              # recording had no genre/tag, so use the artist's
        genre = _artist_genre(artist_mbid)
    return (genre, year)


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, should_stop=None, pending=None):
    """Walk a track set's not-yet-enriched tracks, fetch genre+year for each, persist, and report
    progress. Scope is a playlist (playlist_id) or an explicit `pending` list (e.g. an album's tracks).
    `on_progress(event_dict)` receives info/track/done events for the SSE stream."""
    enrich_fn = enrich_fn or enrich_full   # resolved here so tests can monkeypatch module-level enrich_full
    pending = store.tracks_to_enrich(playlist_id) if pending is None else pending

    def _per_item(i, total, t):
        genre, year, mbid, _rel = enrich_fn(t["title"], t["artist"])   # one search: genre, year, MBID
        if _breaker.tripped():             # host unreachable, so the rest would all fail too: stop
            on_progress({"type": "err", "text": "MusicBrainz looks unreachable; enrichment stopped. "
                         "The remaining tracks will retry next time."})
            return False
        if mbid:
            store.set_track_mbid(t["id"], mbid)
        store.set_track_enrichment(t["id"], genre, year)
        eff_genre, eff_year = store.get_track_enrichment(t["id"])    # report what actually stuck
        bits = " · ".join(x for x in (genre, year) if x) or "no match"
        on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                     "genre": eff_genre, "year": eff_year,
                     "text": f"{i}/{total} {t['title']}: {bits}"})

    run_enrich_loop(
        store, on_progress, pending, gate=_gate, breaker=_breaker, should_stop=should_stop,
        empty_text="Everything is already enriched.",
        start_text=lambda n: f"Enriching {n} track(s) via MusicBrainz…",
        done_text=lambda n: f"Enriched {n} track(s).",
        wait_text="Waiting: a newer playlist is enriching first…",
        per_item=_per_item)
