"""Road-trip candidate acquisition, library eligibility and provider metadata.

This owner may call providers. It never allocates a mix, changes draft state, or
writes playlists; the draft application service passes its snapshots to the mixer.
"""
import math
from collections import Counter
from dataclasses import dataclass
from itertools import zip_longest
from typing import Protocol

from yt_playlist.providers import deezer, lastfm, musicbrainz
from yt_playlist.providers import genres as genre_names
from yt_playlist.util import genre_map
from yt_playlist.util.thumbnails import best_thumb

ARTIST_SONGS_LIMIT = 30
GENRE_SEARCH_LIMIT = 30
MIN_ARTIST_CAP = 4
RELATED_ARTISTS = 3
GENRE_ARTISTS = 12
LIKE_BONUS = 8
DURATION_LOOKUPS = 12


class MusicClient(Protocol):
    def search(self, query: str, filter: str = "songs") -> list[dict]: ...
    def get_artist(self, browse_id: str) -> dict: ...
    def get_song(self, video_id: str) -> dict: ...


@dataclass(frozen=True)
class InputRequest:
    kind: str
    name: str
    cap: int
    decade: str | None = None


@dataclass(frozen=True)
class CandidateBatch:
    rows: list[dict]
    related: list[str]


def _decade(year):
    return str(int(year) // 10 * 10) if year else ""


def _canon_genre(genre):
    genre = (genre or "").strip()
    return (genre_names.match_tag(genre) or genre.title()) if genre else ""


def _candidate(video_id, title, artist, album, thumbnail, duration, source, genre, year, plays=0):
    genre = _canon_genre(genre)
    return {"video_id": video_id, "title": title or "", "artist": artist or "",
            "album": album or "", "thumbnail": thumbnail, "duration": duration,
            "source": source, "genre": genre, "family": genre_map.family(genre) if genre else "",
            "year": year, "decade": _decade(year), "plays": plays, "fam": 0.0, "score": 0.0,
            "cluster_genre": ""}


def _rank_scores(cands, value_of):
    size = len(cands)
    if not size:
        return cands
    for index, candidate in enumerate(cands):
        candidate["score"] = 1.0 - (index / size) * 0.75
    ordered = sorted(cands, key=lambda candidate: value_of(candidate) or 0)
    for index, candidate in enumerate(ordered):
        candidate["fam"] = index / (size - 1) if size > 1 else 0.5
    return cands


def own_candidates(store, now, state=None):
    songs = store.rec_query.library_songs()
    plays = store.rec_query.play_counts()
    hard = store.rec_model.suppressed_keys("for_you", now) | store.rec_query.generated_track_keys()
    blocked = (state or {}).get("blacklist_genres") or []
    if blocked:
        hard |= store.rec_query.keys_in_genre_selection(blocked)
    muted = store.rec_model.muted_artists()
    by_artist = store.rec_query.artist_genre_years()
    learned = (state or {}).get("own_facts") or {}
    out = []
    for song in songs:
        if song["key"] in hard or song["artist"] in muted:
            continue
        extra = learned.get(song["video_id"]) or {}
        fallback = by_artist.get(song["artist"]) or {}
        candidate = _candidate(song["video_id"], song["title"], song["artist"], song["album"], song["thumbnail"],
                               song["duration"], "mine", song["genre"] or extra.get("genre") or fallback.get("genre") or "",
                               song["year"] or extra.get("year") or fallback.get("year"), plays.get(song["key"], 0))
        candidate["engagement"] = candidate["plays"] + (LIKE_BONUS if song["liked"] else 0)
        out.append(candidate)
    out.sort(key=lambda candidate: candidate["engagement"])
    for index, candidate in enumerate(out):
        candidate["fam"] = index / (len(out) - 1) if len(out) > 1 else 0.5
        candidate["score"] = 1.0
    return out


_FACTS_CACHE: dict = {}
_ARTIST_GENRE_CACHE: dict = {}
_GENRE_ARTIST_CACHE: dict = {}
_NOT_AN_ARTIST = {"various artists", "various", "[unknown]", "unknown artist", "soundtrack",
                  "traditional", "[no artist]", "va"}


def _facts(title, artist):
    key = (title or "", artist or "")
    if key not in _FACTS_CACHE:
        if len(_FACTS_CACHE) > 4000:
            _FACTS_CACHE.clear()
        _FACTS_CACHE[key] = deezer.lookup(title, artist)
    return _FACTS_CACHE[key]


def artist_genre(store, artist):
    if not artist:
        return None
    if artist not in _ARTIST_GENRE_CACHE:
        key = lastfm.api_key(store)
        _ARTIST_GENRE_CACHE[artist] = lastfm.artist_genre(artist, key) if key else None
    return _ARTIST_GENRE_CACHE[artist]


def _artist_page(client, name):
    try:
        results = client.search(name, filter="artists") or []
    except Exception:
        return None, None
    browse_id = results[0].get("browseId") if results else None
    if not browse_id:
        return None, None
    try:
        return (client.get_artist(browse_id) or {}), browse_id
    except Exception:
        return None, None


def _page_songs(page, fallback_name, limit=ARTIST_SONGS_LIMIT):
    rows = (((page or {}).get("songs") or {}).get("results")) or []
    out = []
    for row in rows[:limit]:
        if not (video_id := row.get("videoId")):
            continue
        artists = row.get("artists") or []
        out.append({"video_id": video_id, "title": row.get("title") or "",
                    "artist": (artists[0].get("name") if artists else None) or fallback_name,
                    "album": (row.get("album") or {}).get("name") or "",
                    "thumbnail": best_thumb(row.get("thumbnails")), "duration": row.get("duration_seconds"), "genre": ""})
    return out


def _artist_songs(client, name, limit=ARTIST_SONGS_LIMIT):
    page, _ = _artist_page(client, name)
    return _page_songs(page, name, limit)


def _related_names(page, limit=RELATED_ARTISTS):
    rows = (((page or {}).get("related") or {}).get("results")) or []
    return [row.get("title") for row in rows[:limit] if row.get("title")]


def _genre_songs(client, genre, limit=GENRE_SEARCH_LIMIT):
    try:
        results = client.search(genre, filter="songs") or []
    except Exception:
        return []
    out = []
    for row in results[:limit]:
        if not (video_id := row.get("videoId")):
            continue
        artists = row.get("artists") or []
        out.append({"video_id": video_id, "title": row.get("title") or "",
                    "artist": (artists[0].get("name") if artists else "") or "",
                    "album": (row.get("album") or {}).get("name") or "",
                    "thumbnail": best_thumb(row.get("thumbnails")), "duration": row.get("duration_seconds"), "genre": genre})
    return out


def _cap_per_artist(rows, cap):
    per, out = Counter(), []
    for row in rows:
        if per[row["artist"]] < cap:
            per[row["artist"]] += 1
            out.append(row)
    return out


def genre_artists(store, genre, decade=None, limit=GENRE_ARTISTS):
    cache_key = (genre.lower(), decade)
    if cache_key in _GENRE_ARTIST_CACHE:
        return _GENRE_ARTIST_CACHE[cache_key]
    key = lastfm.api_key(store)
    ranked = lastfm.tag_top_artists(genre, key, limit=limit * 2) if key else []
    if decade:
        dated = musicbrainz.tag_artists(genre, decade=decade, limit=limit * 2)
        agreed = {name.lower() for name in dated}
        both = [name for name in ranked if name.lower() in agreed]
        rest_ranked = [name for name in ranked if name.lower() not in agreed]
        rest_dated = [name for name in dated if name.lower() not in {other.lower() for other in both}]
        names = both + [name for pair in zip_longest(rest_ranked, rest_dated) for name in pair if name]
    else:
        names = ranked or musicbrainz.tag_artists(genre, limit=limit)
    out, seen = [], set()
    for name in names:
        low = (name or "").strip().lower()
        if low and low not in seen and low not in _NOT_AN_ARTIST:
            seen.add(low)
            out.append(name.strip())
    _GENRE_ARTIST_CACHE[cache_key] = out[:limit]
    return out[:limit]


def other_input_songs(client, kind, name, cap, store=None, decade=None):
    if kind == "artist":
        page, _ = _artist_page(client, name)
        return _cap_per_artist(_page_songs(page, name), cap), _related_names(page)
    rows, artists = [], genre_artists(store, name, decade)
    per_artist = max(2, math.ceil(cap / max(1, min(len(artists), GENRE_ARTISTS))))
    for artist in artists:
        if len(rows) >= cap:
            break
        for row in _cap_per_artist(_artist_songs(client, artist), per_artist):
            row["genre"] = row["genre"] or name
            rows.append(row)
    if not rows:
        rows = _cap_per_artist(_genre_songs(client, name), cap)
    return rows[:cap], []


def acquire_input(client: MusicClient, store, request: InputRequest) -> CandidateBatch:
    rows, related = other_input_songs(client, request.kind, request.name, request.cap, store, request.decade)
    return CandidateBatch(rows, related)


def to_candidates(rows, store=None):
    out = []
    for row in rows:
        facts = _facts(row["title"], row["artist"])
        genre = artist_genre(store, row["artist"]) or row["genre"] or facts.get("genre") or ""
        candidate = _candidate(row["video_id"], row["title"], row["artist"], row["album"], row["thumbnail"],
                               row["duration"] or facts.get("duration"), "theirs", genre, facts.get("year"))
        candidate["input"] = row.get("input")
        out.append(candidate)
    return out


def rank_other(cands):
    def popularity(candidate):
        return _facts(candidate["title"], candidate["artist"]).get("popularity")
    return _rank_scores(sorted(cands, key=lambda candidate: -(popularity(candidate) or 0)), popularity)


def other_cap(artists, genres, limit):
    inputs = [name for name in (artists or []) if name] + [name for name in (genres or []) if name]
    return max(MIN_ARTIST_CAP, math.ceil(limit / max(1, len(inputs))))


def build_other_pool(client, artists, genres, limit, store=None):
    cap = other_cap(artists, genres, limit)
    rows, seen, related = [], set(), []

    def take(candidates):
        for candidate in candidates:
            if candidate["video_id"] not in seen:
                seen.add(candidate["video_id"])
                rows.append(candidate)

    for kind, names in (("artist", artists or []), ("genre", genres or [])):
        for name in names:
            chunk, more = other_input_songs(client, kind, name, cap, store)
            for row in chunk:
                row["input"] = name
            take(chunk)
            related += more
    for name in related:
        if len(rows) >= limit:
            break
        take(_cap_per_artist(_artist_songs(client, name), max(2, cap // 2)))
    return rank_other(to_candidates(rows[:limit], store))


def _resolve_durations(store, client, pool, cap=DURATION_LOOKUPS):
    spent = 0
    for candidate in pool:
        if candidate["duration"] is not None:
            continue
        candidate["duration"] = store.tracks.known_duration(candidate["title"], candidate["artist"])
        if candidate["duration"] is not None or spent >= cap or client is None:
            continue
        spent += 1
        try:
            details = (client.get_song(candidate["video_id"]) or {}).get("videoDetails") or {}
            seconds = details.get("lengthSeconds")
            candidate["duration"] = int(seconds) if seconds not in (None, "") else None
        except Exception:
            candidate["duration"] = None
    return pool
