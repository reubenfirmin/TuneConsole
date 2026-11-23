"""Road-trip candidate acquisition, library eligibility and provider metadata.

This owner may call providers. It never allocates a mix, changes draft state, or
writes playlists; the draft application service passes its snapshots to the mixer.
"""
import json
import math
from collections import Counter, OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from itertools import zip_longest
from threading import Lock
from time import monotonic
from typing import Protocol

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.providers import deezer, genres, lastfm, musicbrainz
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.rec import artist_model
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.util import genre_map, net
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
    browse_id: str | None = None


@dataclass(frozen=True)
class CandidateBatch:
    rows: list[dict]
    related: list[dict]


def _decade(year):
    return str(int(year) // 10 * 10) if year else ""


def _candidate(video_id, title, artist, album, thumbnail, duration, source, genre, year, plays=0):
    genre = canonical_genre(genre)
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
    """Detach eligible rows; the transition resolves their current families in one batch."""
    songs = store.rec_query.library_songs()
    plays = store.rec_query.play_counts()
    hard = store.rec_model.suppressed_keys("for_you", now)
    blocked = (state or {}).get("blacklist_genres") or []
    if blocked:
        hard |= store.rec_query.keys_in_genre_selection(blocked)
    muted = store.rec_model.muted_artists()
    by_artist = store.rec_query.artist_genre_years()
    learned = (state or {}).get("own_facts") or {}
    return own_rows(songs, plays, hard, muted, by_artist, learned)


def own_rows(songs, plays, hard, muted, by_artist, learned):
    """Interpret eligible library rows without acquiring any inputs."""
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
        candidate["key"] = song["key"]
        candidate['artist_ids'] = song.get('artist_ids', [])
        out.append(candidate)
    out.sort(key=lambda candidate: candidate["engagement"])
    for index, candidate in enumerate(out):
        candidate["fam"] = index / (len(out) - 1) if len(out) > 1 else 0.5
        candidate["score"] = 1.0
    return out


_FACTS_CACHE = OrderedDict()
_ARTIST_GENRE_CACHE = OrderedDict()
_GENRE_ARTIST_CACHE = OrderedDict()
_METADATA_CACHE_LOCK = Lock()
_METADATA_CACHE_LIMIT = 512
_METADATA_TTL = 86400
_METADATA_RETRY_TTL = 60
_NOT_AN_ARTIST = {"various artists", "various", "[unknown]", "unknown artist", "soundtrack",
                  "traditional", "[no artist]", "va"}


def _facts(title, artist):
    key = (title or "", artist or "")

    def load():
        facts = deezer.lookup(title, artist)
        # Domain facts retain their short incomplete-result window; vendor access,
        # shared raw responses and retry admission belong to Deezer's IAO.
        complete = all(facts.get(field) not in (None, "")
                       for field in ("duration", "year", "genre", "popularity"))
        return facts, complete

    return dict(_metadata_cached(_FACTS_CACHE, key, load))


def track_facts(store, title, artist):
    """Obtain derived facts with the caller's durable vendor-access scope."""
    with deezer.access(store):
        return _facts(title, artist)


def artist_genre(store, artist):
    if not artist:
        return None
    if artist.startswith('@artist:'):
        counts = Counter(row['genre'] for row in store.rec_query.tracks_by_artists([artist]) if row['genre'])
        return counts.most_common(1)[0][0] if counts else None
    key = lastfm.api_key(store)
    if not key:
        return None  # Missing credentials must never poison a later configured lookup.
    matcher = genres.snapshot(store)
    vocabulary = sha256(json.dumps([dict(matcher.canon), dict(matcher.keys)], sort_keys=True).encode()).digest()
    cache_key = (artist.casefold(), _credential_version(key), vocabulary)
    with lastfm.access(store):
        return _metadata_cached(_ARTIST_GENRE_CACHE, cache_key,
                                lambda: (lastfm.artist_genre(artist, key, matcher=matcher), True))


def _credential_version(key):
    return sha256(key.encode()).digest() if key else None


def _metadata_cached(cache, key, load):
    """Bounded LRU retention; ambiguous provider misses retry after one minute.

    These are derived domain results; raw Last.fm responses belong to its IAO.
    Partial fallback results and unavailable refreshes use the retry window, and
    their retry evidence survives a cache hit. Never hold this lock across I/O.
    """
    with _METADATA_CACHE_LOCK:
        cached = cache.get(key)
        if cached and monotonic() < cached[0]:
            cache.move_to_end(key)
            for error in cached[2]:
                net.record_probe_failure(error)
            return cached[1]
    with net.probe_errors() as errors:
        value, complete = load()
    for error in errors:
        net.record_probe_failure(error)
    if errors and not value and cached:
        value = cached[1]
    ttl = _METADATA_TTL if value and complete and not errors else _METADATA_RETRY_TTL
    # Retain retry evidence without keeping exception tracebacks, response buffers
    # or a Store alive through this bounded domain cache.
    retained_errors = tuple(IntegrationUnavailable(error.provider, str(error), error.retry_at,
                            outage=error.outage) if isinstance(error, IntegrationUnavailable)
                            else str(error) for error in errors)
    with _METADATA_CACHE_LOCK:
        cache[key] = (monotonic() + ttl, value, retained_errors)
        cache.move_to_end(key)
        while len(cache) > _METADATA_CACHE_LIMIT:
            cache.popitem(last=False)
    return value


def _artist_page(client, name, browse_id=None):
    try:
        if not browse_id:
            results = client.search(name, filter="artists") or []
            browse_id = results[0].get("browseId") if results else None
        if not browse_id:
            return None, None
        return (client.get_artist(browse_id) or {}), browse_id
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        return None, browse_id


def _page_songs(page, fallback_name, limit=ARTIST_SONGS_LIMIT, *, browse_id=None):
    rows = (((page or {}).get("songs") or {}).get("results")) or []
    out = []
    for row in rows[:limit]:
        if not (video_id := row.get("videoId")):
            continue
        credits = row.get("artists") or ()
        credit = next(iter(credits), {})
        artist = credit.get("name") or fallback_name
        channel = credit.get("id") or credit.get("browseId")
        if browse_id and any((c.get('id') or c.get('browseId')) == browse_id for c in credits):
            channel = browse_id
        if not channel and artist.casefold() == fallback_name.casefold():
            channel = browse_id
        out.append({"video_id": video_id, "title": row.get("title") or "",
                    "artist": artist, "artist_browse_id": channel,
                    "album": (row.get("album") or {}).get("name") or "",
                    "thumbnail": best_thumb(row.get("thumbnails")), "duration": row.get("duration_seconds"), "genre": ""})
    return out


def artist_songs(client, name, limit=ARTIST_SONGS_LIMIT, *, browse_id=None):
    page, browse = _artist_page(client, name, browse_id) if browse_id else _artist_page(client, name)
    return _page_songs(page, name, limit, browse_id=browse)


def _related_artists(store, name, browse, page, limit):
    if store is None:
        return []
    seed = artist_model.youtube_artist_key(store.artists, browse) if browse else store.artists.key(name) or name
    listed = [(rank, ref) for rank, ref in artist_model.youtube_neighbors(page, allow_names=True)
              if not browse or ref['browse_id'] != browse]
    ranked = artist_model.ranked_artist_neighbors(store, [seed], youtube=listed, topn=limit)
    result = []
    for neighbor in ranked:
        if reference := artist_model.neighbor_reference(store.artists, neighbor):
            result.append({**reference, 'key': neighbor.key, 'sources': neighbor.sources})
    return result


def artist_context(client: MusicClient, name: str, store=None, *, browse_id=None) -> CandidateBatch:
    """The seed's representative songs and neighborhood, including for legacy draft repair."""
    page, browse = _artist_page(client, name, browse_id) if browse_id else _artist_page(client, name)
    return CandidateBatch(_page_songs(page, name, limit=5, browse_id=browse),
                          _related_artists(store, name, browse, page, GENRE_ARTISTS))


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
    if limit <= 0:
        return []
    key = lastfm.api_key(store)
    cache_key = (genre.casefold(), decade, limit, _credential_version(key))
    names = _metadata_cached(_GENRE_ARTIST_CACHE, cache_key,
                             lambda: _load_genre_artists(genre, decade, limit, key, store))
    return list(names)  # Callers must not mutate a cached result.


def _load_genre_artists(genre, decade, limit, key, store):
    with lastfm.access(store):
        ranked = lastfm.tag_top_artists(genre, key, limit=limit * 2) if key else []
    complete = bool(ranked) or not key
    if decade:
        dated = musicbrainz.tag_artists(genre, decade=decade, limit=limit * 2, store=store)
        complete = complete and bool(dated)
        agreed = {name.lower() for name in dated}
        both = [name for name in ranked if name.lower() in agreed]
        rest_ranked = [name for name in ranked if name.lower() not in agreed]
        rest_dated = [name for name in dated if name.lower() not in {other.lower() for other in both}]
        names = both + [name for pair in zip_longest(rest_ranked, rest_dated) for name in pair if name]
    else:
        names = ranked or musicbrainz.tag_artists(genre, limit=limit, store=store)
    out, seen = [], set()
    for name in names:
        low = (name or "").strip().lower()
        if low and low not in seen and low not in _NOT_AN_ARTIST:
            seen.add(low)
            out.append(name.strip())
    return out[:limit], complete


def other_input_songs(client, kind, name, cap, store=None, decade=None, *, browse_id=None):
    if kind == "artist":
        page, browse = _artist_page(client, name, browse_id) if browse_id else _artist_page(client, name)
        rows = _page_songs(page, name, browse_id=browse)
        if browse_id:
            rows = [row for row in rows if row['artist_browse_id'] == browse_id]
        return (_cap_per_artist(rows, cap),
                _related_artists(store, name, browse, page, RELATED_ARTISTS))
    rows, artists = [], genre_artists(store, name, decade)
    per_artist = max(2, math.ceil(cap / max(1, min(len(artists), GENRE_ARTISTS))))
    for artist in artists:
        if len(rows) >= cap:
            break
        for row in _cap_per_artist(artist_songs(client, artist), per_artist):
            row["genre"] = row["genre"] or name
            rows.append(row)
    if not rows:
        rows = _cap_per_artist(_genre_songs(client, name), cap)
    return rows[:cap], []


def acquire_input(client: MusicClient, store, request: InputRequest) -> CandidateBatch:
    options = {'browse_id': request.browse_id} if request.browse_id else {}
    rows, related = other_input_songs(client, request.kind, request.name, request.cap, store, request.decade, **options)
    return CandidateBatch(rows, related)


def to_candidates(rows, store=None):
    facts = [track_facts(store, row['title'], row['artist']) for row in rows]
    artist_genres = {row['artist']: artist_genre(store, row['artist']) for row in rows}
    out = candidate_rows(rows, facts, artist_genres)
    if store is not None:
        axes = store.genres.feature_axes(candidate['genre'] for candidate in out)
        for candidate in out:
            candidate['family'] = axes.get(candidate['genre'], ('', None))[0]
    return out


def candidate_rows(rows, metadata, artist_genres):
    out = []
    for row, facts in zip(rows, metadata, strict=True):
        genre = artist_genres.get(row['artist']) or row['genre'] or facts.get('genre') or ''
        candidate = _candidate(row["video_id"], row["title"], row["artist"], row["album"], row["thumbnail"],
                               row["duration"] or facts.get("duration"), "theirs", genre, facts.get("year"))
        candidate["input"] = row.get("input")
        if row.get('artist_browse_id'):
            candidate['artist_browse_id'] = row['artist_browse_id']
        out.append(candidate)
    return out


def rank_other(cands, store=None):
    popularity = {c['video_id']: track_facts(store, c['title'], c['artist']).get('popularity') for c in cands}
    return rank_rows(cands, popularity)


def rank_rows(cands, popularity):
    value = lambda candidate: popularity.get(candidate['video_id']) or 0
    return _rank_scores(sorted(cands, key=lambda candidate: -value(candidate)), value)


def other_cap(artists, genres, limit):
    inputs = [name for name in (artists or []) if name] + [name for name in (genres or []) if name]
    return max(MIN_ARTIST_CAP, math.ceil(limit / max(1, len(inputs))))


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
