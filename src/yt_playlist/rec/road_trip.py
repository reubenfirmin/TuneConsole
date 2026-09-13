"""Road Trip playlist generation: blend your taste-weighted tracks (the 'own' pool) with popular
tracks pulled from YouTube for other people's artists/genres (the 'other' pool), mixed to a target
ratio and cut to a target duration. Neither pool needs bespoke taste-model handling: the result is
materialized via executor.create_generated_playlist with the normal Generated group, which already
quarantines it from every taste signal and schedules it for GC.

The unit of work is a DRAFT (see repos/road_trip.py). The two sides are not symmetric:

  · YOURS is your whole collection, read from the library on every re-pick (~30ms of local SQL) and
    stratified by engagement - plays plus likes. There is no sample and so nothing to run out of:
    "make it 40% rock" is a question about your library, not about a lucky draw from it.
  · THEIRS is a bounded pool assembled over the network (YouTube pages, Deezer/Last.fm lookups), so
    it IS cached on the draft, grown in the background, and widened on demand when a slider asks for
    more than it holds.

Everything after the build - the mine/theirs mix, the familiarity lean, the genre and year bars,
crossing a slot out - re-picks with no network at all, which is what makes the panel feel live.

Picking is weighted-random (Efraimidis-Spirakis), not top-N, so the same recipe run four times gives
four different playlists: the seed is fresh per build, and the previous build's tracks are penalized.
"""
import math
import random
import statistics
from collections import Counter, deque
from itertools import zip_longest

# `genre_names` alias: `genres` is a local/parameter name all over this module (the recipe's
# genre inputs, a track->genre map), and shadowing the provider would be a quiet trap.
from yt_playlist.providers import deezer, lastfm, musicbrainz
from yt_playlist.providers import genres as genre_names
from yt_playlist.rec.journeys import journey_order
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_map
from yt_playlist.util.thumbnails import best_thumb

ARTIST_SONGS_LIMIT = 30    # top songs pulled per artist input, before the diversity cap
GENRE_SEARCH_LIMIT = 30    # search results pulled per genre input, before the diversity cap
MIN_ARTIST_CAP = 4         # a listed artist always gets at least this many candidates in the pool
RELATED_ARTISTS = 3        # related artists borrowed per listed artist when their pool runs thin
MAX_OTHER_POOL = 44        # ceiling on their pool: every candidate costs a Deezer lookup
AVG_TRACK_S = 210          # duration assumed for a track whose length nothing knows
POOL_SLACK = 3.0           # candidates per slot, so sliders/rerolls have somewhere to go
FAMILIARITY_SIGMA = 0.3    # width of the familiarity band the slider selects
REPEAT_PENALTY = 0.3       # weight multiplier for a track the previous build already used
DURATION_LOOKUPS = 12      # cap on live get_song calls per build (durations are a nicety)
OWN_FACT_LOOKUPS = 45      # cap on Deezer lookups spent tagging your own untagged candidates
GENRE_BARS = 8             # genre sliders shown per party (plus any you've already pinned)
GENRE_ARTISTS = 12         # artists a genre resolves to before YouTube is asked for tracks
LIKE_BONUS = 8             # plays a Liked song is worth: a like is deliberate, a play can be idle

# What a picked row carries into the draft (and on to YouTube). Everything the panel renders plus
# what the ordering and the bars need, and nothing else - the pool entries also hold scoring scratch.
_ROW_FIELDS = ("video_id", "title", "artist", "album", "thumbnail", "duration", "source",
               "genre", "family", "year", "decade", "cluster_genre")


# --------------------------------------------------------------------------- candidate assembly

def _decade(year):
    """'1990' for 1994, or '' when the year is unknown. The era axis of the sliders."""
    return str(int(year) // 10 * 10) if year else ""


def _canon_genre(genre):
    """One spelling per genre. The same genre reaches a candidate as "Alternative Rock" (Last.fm's
    whitelist), "alternative rock" (what the user typed as a recipe input) or "Rock" (Deezer's album
    genre), and three spellings mean three bars competing for the same tracks - with a quota on one
    doing nothing about the others. Run it through the same whitelist the enrichment providers use,
    and title-case whatever that doesn't recognize."""
    genre = (genre or "").strip()
    if not genre:
        return ""
    return genre_names.match_tag(genre) or genre.title()


def _candidate(video_id, title, artist, album, thumbnail, duration, source, genre, year, plays=0):
    """One pool entry. `fam` (0..1) is filled in later, once the whole side is known: familiarity is
    a track's RANK within the pool, not an absolute play count, so the slider means the same thing
    for a 200-play library and a 20-play one."""
    genre = _canon_genre(genre)
    return {"video_id": video_id, "title": title or "", "artist": artist or "",
            "album": album or "", "thumbnail": thumbnail, "duration": duration,
            "source": source, "genre": genre, "family": genre_map.family(genre) if genre else "",
            "year": year, "decade": _decade(year), "plays": plays, "fam": 0.0, "score": 0.0,
            "cluster_genre": ""}


def _rank_scores(cands, value_of):
    """Fill `score` (base desirability, 1.0 best) and `fam` (familiarity position, 1.0 = the most
    familiar) from each candidate's rank on `value_of`. Rank rather than raw value so one runaway
    play count or Deezer rank can't dominate the draw."""
    n = len(cands)
    if not n:
        return cands
    for i, c in enumerate(cands):
        c["score"] = 1.0 - (i / n) * 0.75          # keep the tail drawable, just less likely
    ordered = sorted(cands, key=lambda c: value_of(c) or 0)
    for i, c in enumerate(ordered):
        c["fam"] = (i / (n - 1)) if n > 1 else 0.5
    return cands


def own_candidates(store, now, state=None):
    """YOUR SIDE: the whole collection, stratified by how much you actually listen to it.

    Not a sample. An earlier version drew a few hundred candidates from the taste surfaces and
    treated that as the pool, which made every genre question a question about the sample instead of
    about your library - ask for 40% rock and you'd get however much rock the sample happened to
    hold. Everything you own is eligible; `fam` (the engagement percentile) is what the
    favorites/deeper-cuts slider slides along, and the genre and year bars pick out of the whole
    thing. Reading the library costs ~30ms, so this is rebuilt per re-pick rather than stored.

    Engagement is play count plus a bonus for a Liked song - a like is a deliberate statement where a
    play can be an accident. `score` is deliberately flat: with the whole collection in play, nothing
    is inherently more "recommended" than anything else, and the slider does the choosing.

    Still filtered by the taste model's own exclusions: songs you dismissed or muted, and anything
    already bundled into a generated playlist, stay out."""
    songs = store.library_songs()
    plays = store.play_counts()
    hard = store.suppressed_keys("for_you", now) | RecDao(store).generated_track_keys()
    blocked_genres = (state or {}).get("blacklist_genres") or []
    if blocked_genres:
        hard |= store.keys_in_genre_selection(blocked_genres)
    muted = store.muted_artists()
    by_artist = store.artist_genre_years()
    learned = (state or {}).get("own_facts") or {}
    out = []
    for s in songs:
        if s["key"] in hard or s["artist"] in muted:
            continue
        # Libraries are genre-tagged in patches (enrichment is incremental) and an untagged track
        # can't appear on any bar: fall back to what this artist's OTHER tracks are tagged as, then
        # to anything Deezer told us during this draft (fill_own_facts).
        extra = learned.get(s["video_id"]) or {}
        fallback = by_artist.get(s["artist"]) or {}
        c = _candidate(s["video_id"], s["title"], s["artist"], s["album"], s["thumbnail"],
                       s["duration"], "mine",
                       s["genre"] or extra.get("genre") or fallback.get("genre") or "",
                       s["year"] or extra.get("year") or fallback.get("year"),
                       plays.get(s["key"], 0))
        c["engagement"] = c["plays"] + (LIKE_BONUS if s["liked"] else 0)
        out.append(c)
    out.sort(key=lambda c: c["engagement"])
    n = len(out)
    for i, c in enumerate(out):
        c["fam"] = (i / (n - 1)) if n > 1 else 0.5
        c["score"] = 1.0
    return out


_FACTS_CACHE: dict = {}          # (title, artist) -> deezer facts, for the life of the process
_ARTIST_GENRE_CACHE: dict = {}   # artist -> Last.fm genre (or None), likewise
_GENRE_ARTIST_CACHE: dict = {}   # (genre, decade) -> ranked artist names, likewise
# Credits a catalogue uses where an artist would go. Chasing these down on YouTube returns
# compilations and karaoke, which is exactly what going via the databases is meant to avoid.
_NOT_AN_ARTIST = {"various artists", "various", "[unknown]", "unknown artist", "soundtrack",
                  "traditional", "[no artist]", "va"}


def _facts(title, artist):
    """Deezer catalogue facts for a candidate: {popularity, year, genre, duration}. Memoized, since
    a rebuild of the same recipe re-draws largely the same artists. Module-level so tests can patch
    it without a real network call (mirrors discover.fetch_artist_info's convention)."""
    ck = (title or "", artist or "")
    if ck not in _FACTS_CACHE:
        if len(_FACTS_CACHE) > 4000:             # a session-length cache, not a leak
            _FACTS_CACHE.clear()
        _FACTS_CACHE[ck] = deezer.lookup(title, artist)
    return _FACTS_CACHE[ck]


def artist_genre(store, artist):
    """A specific, whitelisted genre for an artist from Last.fm ("Alternative Rock"), or None.

    Deezer only knows the ALBUM's genre, which is coarse enough to be useless for steering - it calls
    half a library "Rock" or "Alternativo", so a bar for the genre you actually wanted never appears.
    Last.fm's tags run through the curated whitelist in providers/genres.py, which is where names
    like "Alternative Rock" come from. One call per ARTIST (not per track) and memoized, so their
    side costs a handful of requests. Silent None when no API key is configured."""
    if not artist:
        return None
    if artist not in _ARTIST_GENRE_CACHE:
        key = lastfm.api_key(store)
        _ARTIST_GENRE_CACHE[artist] = lastfm.artist_genre(artist, key) if key else None
    return _ARTIST_GENRE_CACHE[artist]


def _artist_page(client, name):
    """(artist page dict, browse_id) for an artist name, or (None, None) if unresolvable."""
    try:
        results = client.search(name, filter="artists") or []
    except Exception:  # noqa: BLE001 - network/parse all degrade to "no songs found"
        return None, None
    browse_id = results[0].get("browseId") if results else None
    if not browse_id:
        return None, None
    try:
        return (client.get_artist(browse_id) or {}), browse_id
    except Exception:  # noqa: BLE001
        return None, None


def _page_songs(page, fallback_name, limit=ARTIST_SONGS_LIMIT):
    """Normalized track dicts from an artist page's top songs (already popularity-ranked by YouTube).

    artist["songs"]["results"] rows go through ytmusicapi's parse_playlist_item (browsing.py:300 ->
    parsers/playlists.py), NOT the simplified shape shown in get_artist's own docstring: "artists"
    is a LIST of {"name","id"} dicts (parse_song_artists), and "album" is a {"name","id"} dict or
    None (parse_song_album) - never a plain string. duration_seconds is present when a duration was
    found, same field name as search results."""
    rows = (((page or {}).get("songs") or {}).get("results")) or []
    out = []
    for row in rows[:limit]:
        vid = row.get("videoId")
        if not vid:
            continue
        artists = row.get("artists") or []
        out.append({"video_id": vid, "title": row.get("title") or "",
                    "artist": (artists[0].get("name") if artists else None) or fallback_name,
                    "album": (row.get("album") or {}).get("name") or "",
                    "thumbnail": best_thumb(row.get("thumbnails")),
                    "duration": row.get("duration_seconds"), "genre": ""})
    return out


def _artist_songs(client, name, limit=ARTIST_SONGS_LIMIT):
    """Top songs for an artist, via YouTube Music's own artist page. [] if unresolvable."""
    page, _ = _artist_page(client, name)
    return _page_songs(page, name, limit)


def _related_names(page, limit=RELATED_ARTISTS):
    """Names of artists YouTube considers related to this one, for widening a thin pool."""
    rows = (((page or {}).get("related") or {}).get("results")) or []
    return [r.get("title") for r in rows[:limit] if r.get("title")]


def _genre_songs(client, genre, limit=GENRE_SEARCH_LIMIT):
    """Songs matching a genre term via YouTube Music search. No artist anchor exists for a genre
    input, so this searches the term directly rather than resolving via get_artist. The search term
    is kept as the candidate's genre: it is the only genre signal a bare search result carries, and
    it is what the user asked for.

    Deliberately does not pass `limit=` through to client.search(): FakeClient.search() (the test
    double used throughout this suite) only accepts (query, filter), and the post-slice below
    already caps the result count, so nothing is lost by relying on that slice alone."""
    try:
        results = client.search(genre, filter="songs") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in results[:limit]:
        vid = row.get("videoId")
        if not vid:
            continue
        artists = row.get("artists") or []
        out.append({"video_id": vid, "title": row.get("title") or "",
                    "artist": (artists[0].get("name") if artists else "") or "",
                    "album": (row.get("album") or {}).get("name") or "",
                    "thumbnail": best_thumb(row.get("thumbnails")),
                    "duration": row.get("duration_seconds"), "genre": genre})
    return out


def _cap_per_artist(rows, cap):
    """Keep at most `cap` rows per artist, preserving YouTube's own (popularity) order."""
    per, out = Counter(), []
    for r in rows:
        if per[r["artist"]] >= cap:
            continue
        per[r["artist"]] += 1
        out.append(r)
    return out


def genre_artists(store, genre, decade=None, limit=GENRE_ARTISTS):
    """Who to play for a genre (and optionally a decade): ranked artist names, best source first.

    Last.fm's tag.getTopArtists is the primary source - "the big alternative rock artists" is exactly
    what its listening data knows. MusicBrainz fills in behind it, and is the only one that can say
    "released in the 2000s", so it leads when a decade is asked for. Memoized per (genre, decade).

    This exists because searching YouTube for a genre STRING is a poor way to find music: the results
    are mixes, karaoke, "top 100" uploads and whatever else matched the words. Asking a music
    database who plays the genre, and then asking YouTube for those artists' biggest tracks, uses
    each source for what it is actually good at."""
    ck = (genre.lower(), decade)
    if ck in _GENRE_ARTIST_CACHE:
        return _GENRE_ARTIST_CACHE[ck]
    key = lastfm.api_key(store)
    ranked = lastfm.tag_top_artists(genre, key, limit=limit * 2) if key else []
    if decade:
        # MusicBrainz knows WHO released in the decade but not who matters; Last.fm knows who
        # matters but can't filter by date. Lead with the artists both agree on - ranked by Last.fm,
        # dated by MB - then INTERLEAVE what's left of each, because neither source wins in general:
        # taking Last.fm's tail first buries 80s synthpop under today's synthpop revival, and taking
        # MB's first buries Weezer under whoever else happened to release in 2003. Interleaving hedges,
        # and costs nothing to be wrong about: arrivals are filtered on the track's own year anyway.
        dated = musicbrainz.tag_artists(genre, decade=decade, limit=limit * 2)
        agreed = {n.lower() for n in dated}
        both = [n for n in ranked if n.lower() in agreed]
        rest_ranked = [n for n in ranked if n.lower() not in agreed]
        rest_dated = [n for n in dated if n.lower() not in {b.lower() for b in both}]
        names = both + [n for pair in zip_longest(rest_ranked, rest_dated) for n in pair if n]
    else:
        names = ranked or musicbrainz.tag_artists(genre, limit=limit)
    out, seen = [], set()
    for n in names:
        low = (n or "").strip().lower()
        if not low or low in seen or low in _NOT_AN_ARTIST:
            continue
        seen.add(low)
        out.append(n.strip())
    out = out[:limit]
    _GENRE_ARTIST_CACHE[ck] = out
    return out


def other_input_songs(client, kind, name, cap, store=None, decade=None):
    """The YouTube rows for ONE input of a recipe (an artist or a genre), capped per artist. Returns
    (rows, related_artist_names) - `related` is only non-empty for an artist input, and is how a thin
    pool widens one hop out.

    A genre resolves to its top ARTISTS first (genre_artists), and each of those to their top tracks
    off YouTube's own artist page, which is properly popularity-ranked. A bare YouTube song search
    for the genre name is the last resort, for a genre no database recognizes.

    One input is the unit of incremental building: the draft grows by a chunk per input, so the
    playlist starts filling in as soon as the first artist resolves rather than after all of them."""
    if kind == "artist":
        page, _ = _artist_page(client, name)
        return _cap_per_artist(_page_songs(page, name), cap), _related_names(page)
    rows, artists = [], genre_artists(store, name, decade)
    per_artist = max(2, math.ceil(cap / max(1, min(len(artists), GENRE_ARTISTS))))
    for artist in artists:
        if len(rows) >= cap:
            break
        for row in _cap_per_artist(_artist_songs(client, artist), per_artist):
            row["genre"] = row["genre"] or name       # the genre that brought this artist here
            rows.append(row)
    if not rows:
        rows = _cap_per_artist(_genre_songs(client, name), cap)
    return rows[:cap], []


def to_candidates(rows, store=None):
    """Turn raw YouTube rows into pool candidates, filling genre/year/duration in. This is the
    network-heavy step: one Deezer lookup per row (memoized), which is why their pool is capped.

    Genre, best source first: Last.fm's whitelisted artist genre (specific enough to steer by -
    "Alternative Rock"), then the search term that found the track, then Deezer's album genre."""
    out = []
    for r in rows:
        facts = _facts(r["title"], r["artist"])
        genre = artist_genre(store, r["artist"]) or r["genre"] or facts.get("genre") or ""
        cand = _candidate(
            r["video_id"], r["title"], r["artist"], r["album"], r["thumbnail"],
            r["duration"] or facts.get("duration"), "theirs", genre, facts.get("year"))
        cand["input"] = r.get("input")
        out.append(cand)
    return out


def _remember_artist_context(state, name, candidates, related):
    """The seed's genre and representative era come from its own top songs, not the whole genre."""
    songs = [c for c in candidates if c["artist"].casefold() == name.casefold()]
    genres = Counter(c["genre"] for c in songs if c["genre"])
    years = [c["year"] for c in songs[:5] if c.get("year")]
    state.setdefault("artist_contexts", {})[name] = {
        "genre": genres.most_common(1)[0][0] if genres else "",
        "year": round(statistics.median(years)) if years else None,
        "related": list(related),
    }


def _genre_contexts(state, genre):
    """Only a seed's own genre makes it an anchor for a slider. A shared family is too broad:
    Indie Rock, Alternative Rock and Post-Punk must not all acquire the same seed artists."""
    genre = _canon_genre(genre)
    contexts = {name: context for name, context in state.get("artist_contexts", {}).items()
                if name in state.get("inputs", {}).get("artists", [])}
    return {name: context for name, context in contexts.items()
            if genre and _canon_genre(context["genre"]) == genre}


def _genre_input(state, genre):
    """Retain the explicit genre input behind its tracks and any later slider widening."""
    genres = {_canon_genre(g): g for g in state.get("inputs", {}).get("genres", [])}
    genre = _canon_genre(genre)
    if genre in genres:
        return genres[genre]
    for candidate in state["pool"]:
        origin = _canon_genre(candidate.get("genre_input") or candidate.get("input"))
        if _axis_genre(candidate) == genre and origin in genres:
            return genres[origin]
    return None


def _repair_artist_clusters(state):
    """Repair cached family-wide assignments and remove tracks whose input has left the recipe.

    Work from actual artist relationships, rather than trusting an old slider label or anchors.
    Wait for all seed contexts before pruning legacy tracks with no recorded provenance.
    """
    artists = state.get("inputs", {}).get("artists", [])
    contexts = {n: c for n, c in state.get("artist_contexts", {}).items() if n in artists}
    if not contexts or any(n not in contexts or not contexts[n]["genre"] for n in artists):
        return False
    explicit = {_canon_genre(g) for g in state["inputs"].get("genres", [])}
    kept, removed_inputs, changed = [], set(), False
    for candidate in state["pool"]:
        origin = candidate.get("genre_input") or candidate.get("input")
        if candidate["source"] == "mine" or not origin:
            kept.append(candidate)
            continue
        direct = next((n for n in contexts if n.casefold() == candidate["artist"].casefold()), None)
        anchors = ([direct] if direct else _cluster_anchors(candidate, contexts))
        anchors = [n for n in anchors if not contexts[n]["genre"] or not candidate["genre"]
                   or genre_map.family(contexts[n]["genre"]) == candidate["family"]]
        if anchors:
            matching = [n for n in anchors if n in _genre_contexts(state, candidate.get("cluster_genre"))]
            if not matching:
                matching = [n for n in anchors if n in _genre_contexts(state, candidate["genre"])]
            genre = _canon_genre(contexts[(matching or anchors)[0]]["genre"]) or candidate["genre"]
            anchors = [n for n in anchors if _canon_genre(contexts[n]["genre"]) == genre]
            changed |= candidate.get("cluster_genre") != genre or candidate.get("anchors") != anchors
            candidate.update(cluster_genre=genre, anchors=anchors)
        elif _canon_genre(origin) in explicit and not _genre_contexts(state, _axis_genre(candidate)):
            # An explicit genre without seed anchors is still a valid, independent recipe input.
            changed |= bool(candidate.get("cluster_genre") or candidate.get("anchors"))
            candidate.update(cluster_genre="", anchors=[])
        else:
            removed_inputs.add(origin)
            changed = True
            continue
        kept.append(candidate)
    state["pool"] = kept
    live = {"genre:" + _axis_genre(c) for c in kept if c["source"] == "theirs"}
    axes = state.get("axes", {}).get("theirs", [])
    genres = {a["key"]: a["name"] for a in axes if a["kind"] == "genre"}
    genres.update({k: k.split(":", 1)[1] for k in state.get("targets", {}).get("theirs", {})
                   if k.startswith("genre:")})
    obsolete = {key for key, genre in genres.items() if key not in live
                and not _genre_contexts(state, genre) and _canon_genre(genre) not in explicit}
    if obsolete:
        state["axes"]["theirs"] = [a for a in axes if a["key"] not in obsolete]
        for key in obsolete:
            state.get("targets", {}).get("theirs", {}).pop(key, None)
        state["pending"] = [p for p in state.get("pending", []) if p.get("want") not in obsolete]
        changed = True
    if changed:
        state["done_widening"] = []  # the previous search may have fetched the wrong neighborhood
        state["done_inputs"] = [n for n in state.get("done_inputs", [])
                                if n not in removed_inputs or n in artists or _canon_genre(n) in explicit]
    return changed


def _ensure_artist_contexts(state, store, client):
    """Hydrate older drafts lazily in the background worker, using the same seed artist pages."""
    for name in state.get("inputs", {}).get("artists", []):
        if name in state.get("artist_contexts", {}):
            continue
        page, _ = _artist_page(client, name)
        songs = to_candidates(_page_songs(page, name, limit=5), store)
        _remember_artist_context(state, name, songs, _related_names(page, limit=GENRE_ARTISTS))


def _cluster_anchors(candidate, contexts):
    """One-hop artist similarity plus a ten-year window around each seed's representative era.

    Explicitly selected artists remain eligible throughout their catalog. Related artists need
    dated tracks when the seed has an era; missing dates must not silently admit a modern revival.
    """
    artist = candidate["artist"].casefold()
    anchors = []
    for name, context in contexts.items():
        if artist == name.casefold():
            anchors.append(name)
        elif artist in {a.casefold() for a in context["related"]}:
            year, seed_year = candidate.get("year"), context.get("year")
            if seed_year is None or (year and abs(year - seed_year) <= 10):
                anchors.append(name)
    return anchors


def _cluster_candidates(state, store, client, genre, cap):
    """Grow this recipe's artist neighborhood; None means the genre has no matching artist seed."""
    _ensure_artist_contexts(state, store, client)
    contexts = _genre_contexts(state, genre)
    if not contexts:
        artists = state["inputs"].get("artists", [])
        if (artists and all(state.get("artist_contexts", {}).get(n, {}).get("genre") for n in artists)
                and not _genre_input(state, genre)):
            return []  # an obsolete derived slider must not start a broad genre search
        return None
    genre = _canon_genre(genre)
    family = genre_map.family(genre)
    # Remove broad genre results from older drafts as this slider becomes an anchored cluster.
    kept = []
    for candidate in state["pool"]:
        if candidate["source"] == "theirs" and _matches_axis(candidate, "genre:" + genre):
            anchors = _cluster_anchors(candidate, contexts)
            if not anchors:
                continue
            candidate.update(cluster_genre=genre, anchors=anchors)
        kept.append(candidate)
    state["pool"] = kept
    known = {c["video_id"] for c in kept}
    # Interleave the neighborhoods so one seed cannot consume the entire discovery budget.
    neighborhoods = [[name] + context["related"] for name, context in contexts.items()]
    names = list(dict.fromkeys(n for group in zip_longest(*neighborhoods) for n in group if n))
    per_artist = max(2, math.ceil(cap / max(1, len(names))))
    out = []
    for name in names[:GENRE_ARTISTS]:
        if name in state["inputs"]["artists"] and name not in contexts:
            continue  # another selected seed keeps its own genre, even if a peer list includes it
        rows = [r for r in _artist_songs(client, name) if r["video_id"] not in known]
        accepted = 0
        for candidate in to_candidates(rows[:per_artist * 3], store):
            anchors = _cluster_anchors(candidate, contexts)
            compatible = (not candidate["genre"] or candidate["genre"] == genre
                          or (family and candidate["family"] == family))
            if not anchors or not compatible:
                continue
            candidate.update(cluster_genre=genre, anchors=anchors, input=genre)
            out.append(candidate)
            known.add(candidate["video_id"])
            accepted += 1
            if len(out) >= cap:
                return out
            if accepted >= per_artist:
                break
    return out


def rank_other(cands):
    """(Re)rank their side by Deezer popularity. Popularity is the signal for BOTH scores here:
    their pool has no play history, so "familiar" means "a hit" and "lesser listen" means "a deeper
    cut" - the same slider, applied to their taste instead of yours. Candidates Deezer doesn't know
    keep YouTube's own order, itself a popularity proxy."""
    pop = lambda c: _facts(c["title"], c["artist"]).get("popularity")   # noqa: E731
    return _rank_scores(sorted(cands, key=lambda c: -(pop(c) or 0)), pop)


def other_cap(artists, genres, limit):
    """How many tracks one input may contribute. SCALES WITH DEMAND (limit / number of inputs): a
    fixed small cap is what made a 50% "theirs" mix silently collapse into an almost entirely "mine"
    playlist when the recipe named only one or two artists - there were never enough of their tracks
    to fill the half."""
    inputs = [a for a in (artists or []) if a] + [g for g in (genres or []) if g]
    return max(MIN_ARTIST_CAP, math.ceil(limit / max(1, len(inputs))))


def build_other_pool(client, artists, genres, limit, store=None):
    """Popular tracks for other people's artists/genres, deduped, capped so no one artist dominates,
    and widened to related artists when the listed inputs can't fill the pool. The all-at-once form,
    used when nothing is watching the build progress."""
    cap = other_cap(artists, genres, limit)
    rows, seen, related = [], set(), []

    def _take(candidates):
        for c in candidates:
            if c["video_id"] not in seen:
                seen.add(c["video_id"])
                rows.append(c)

    for kind, names in (("artist", artists or []), ("genre", genres or [])):
        for name in names:
            chunk, more = other_input_songs(client, kind, name, cap, store)
            for row in chunk:
                row["input"] = name       # which recipe input put it here (see apply_recipe)
            _take(chunk)
            related += more

    for name in related:                       # widen: one hop out to related artists
        if len(rows) >= limit:
            break
        _take(_cap_per_artist(_artist_songs(client, name), max(2, cap // 2)))

    return rank_other(to_candidates(rows[:limit], store))


def _resolve_durations(store, client, pool, cap=DURATION_LOOKUPS):
    """Best-effort duration (seconds) for pool entries nothing knows the length of: a duration known
    for the same song under any videoId, else one live lookup (capped, since a wrong estimate only
    costs a slightly-off target length). Done once at build time so every later re-pick can budget
    the mix by duration without touching the network."""
    spent = 0
    for c in pool:
        if c["duration"] is not None:
            continue
        c["duration"] = store.known_duration(c["title"], c["artist"])
        if c["duration"] is not None or spent >= cap or client is None:
            continue
        spent += 1
        try:
            details = (client.get_song(c["video_id"]) or {}).get("videoDetails") or {}
            secs = details.get("lengthSeconds")
            c["duration"] = int(secs) if secs not in (None, "") else None
        except Exception:  # noqa: BLE001 - duration is a nicety; never block generation
            c["duration"] = None
    return pool


# --------------------------------------------------------------------------- picking

def _pool_targets(recipe):
    """(own_pool_size, other_pool_size) for a recipe's target length. Both sides are drawn deeper
    than their current share needs, so moving the mix slider after the build still has candidates to
    reach for without a rebuild. Their side is capped harder: every candidate costs a Deezer lookup,
    and the build has to stay interactive."""
    slots = max(8, math.ceil(recipe["target_minutes"] * 60 / AVG_TRACK_S))
    return (min(140, max(16, int(slots * POOL_SLACK))),
            min(MAX_OTHER_POOL, max(12, int(slots * POOL_SLACK * 0.6))))


def _weight(cand, familiarity, penalized):
    """Draw weight for one candidate: its base rank score, narrowed to the familiarity band the
    slider asks for, and docked if the previous build already used it. Genre and era steering is NOT
    a weight - it's a quota applied at fill time (see _quotas)."""
    w = cand["score"]
    w *= math.exp(-((cand["fam"] - familiarity) ** 2) / (2 * FAMILIARITY_SIGMA ** 2)) + 0.05
    if cand["video_id"] in penalized:
        w *= REPEAT_PENALTY
    return max(w, 0.0)


def _sample_order(cands, rng, weight_of):
    """Weighted random order without replacement (Efraimidis-Spirakis: key = u**(1/w), descending).
    Zero-weight candidates drop out entirely."""
    keyed = []
    for c in cands:
        w = weight_of(c)
        if w <= 0:
            continue
        keyed.append((rng.random() ** (1.0 / w), c))
    keyed.sort(key=lambda t: -t[0])
    return [c for _, c in keyed]


def _artist_cap(cands, needed):
    """How many slots one artist may take. Adapts to the pool: with twenty artists available this is
    2, with a single named artist it is however many the mix needs - the cap exists to stop one
    artist crowding out a broad pool, not to starve a deliberately narrow recipe."""
    artists = {c["artist"] for c in cands if c["artist"]}
    return max(2, math.ceil(needed / max(1, len(artists))) + 1)


def _axis_genre(cand):
    """The genre a candidate is filed under on the sliders: its SPECIFIC genre ("Alternative Rock")
    where one is known, falling back to the coarse family. Specific is the point - "alt rock" is a
    thing people ask for, and a bar labelled with the family it collapses into ("rock-indie") can't
    be asked for at all. Empty string when nothing is tagged."""
    return cand.get("cluster_genre") or cand["genre"] or cand["family"] or ""


def _bucket(cand, kind, quota):
    """Which quota bucket a candidate falls in for `kind`: its own pinned axis, else the shared
    remainder (""). A track with no genre/decade at all always lands in the remainder."""
    key = ("genre:" + _axis_genre(cand)) if kind == "genre" else ("era:" + cand["decade"])
    return key if key in quota else ""


def _prefer_seed_artists(order, artists):
    """Within each genre, offer two named-artist tracks for each discovery track.

    Keep the sampled genre order and each artist's familiarity-weighted track order. Rotate
    between named artists so a large catalogue does not consume another selected artist's share.
    When either group runs out, use the other group's remaining tracks.
    """
    groups = {}
    for candidate in order:
        groups.setdefault(_axis_genre(candidate), []).append(candidate)
    queues = {}
    for genre, rows in groups.items():
        named, related = {}, []
        for candidate in rows:
            artist = candidate["artist"].casefold()
            if artist in artists:
                named.setdefault(artist, []).append(candidate)
            else:
                related.append(candidate)
        seeds = [c for group in zip_longest(*named.values()) for c in group if c]
        queues[genre] = deque(c for group in zip_longest(seeds[::2], seeds[1::2], related)
                              for c in group if c)
    return [queues[_axis_genre(c)].popleft() for c in order]


def _quota_weights(state, party, cands, conditional=False):
    """Requested genre/year shares, optionally conditioned on a library subpool.

    Blend and Yours contain different slices of the library. A 34% Rock library request must not
    cap a Rock-only Blend pool to 34% of its reserved slots. Redistribute absent buckets among the
    available ones, retaining explicit zeroes and the unpinned remainder. Passenger quotas remain
    absolute: a thin requested genre still needs discovery, rather than being silently turned down.
    """
    targets = (state.get("targets") or {}).get(party) or {}
    out = {}
    for kind in ("genre", "era"):
        pins = {k: v for k, v in targets.items() if k.startswith(kind + ":")}
        if not pins:
            continue
        claimed = sum(pins.values())
        if claimed > 1.0:
            pins = {k: v / claimed for k, v in pins.items()}
        out[kind] = {**pins, "": max(0, 1 - claimed)}
    if conditional:
        # Intersect the genre and era restrictions before finding which buckets are available.
        eligible = [c for c in cands if all(q[_bucket(c, kind, q)] > 0
                                           for kind, q in out.items())]
        for kind, weights in out.items():
            available = {_bucket(c, kind, weights) for c in eligible}
            weights.update({k: 0 for k in weights if k not in available})
    return out


def _quotas(state, party, slots, cands, conditional=False):
    """How many of `party`'s tracks each pinned genre/era gets, plus the "" bucket every unpinned
    track shares. Only kinds with at least one pinned slider get a quota; the rest stay free.

    This is what makes a slider mean what it says, in BOTH directions. As a ceiling it holds the
    others back (drag one decade to 100% and the rest empty out). As a floor it is filled first, so
    asking for 40% rock gets 40% rock - weighting alone would just re-order the draw and hand back
    whatever proportion the pool happened to hold, which is not what the number on screen says."""
    return {kind: (_apportion_percentages(weights, slots) if any(weights.values())
                   else dict.fromkeys(weights, 0))
            for kind, weights in _quota_weights(state, party, cands, conditional).items()}


def _fill(order, slots, cap, quotas=None, preferred_artists=()):
    """Fill song slots from a sampled order, honouring artist and genre/era quotas.

    Pinned buckets are filled FIRST, up to their count - a quota is a floor as much as a ceiling.
    Then the remaining slots are filled in sampled order, with every ceiling still enforced.
    Returns the chosen tracks and their duration, which _pick_mix uses to fit the whole trip."""
    quotas = quotas or {}
    picked, per, taken = [], Counter(), set()
    spent = {kind: Counter() for kind in quotas}
    total = 0.0

    def blocked(c):
        if len(picked) >= slots:
            return True
        # Named artists are the core of the genre. The discovery artist cap must not prevent
        # their larger share; _prefer_seed_artists already rotates fairly between them.
        if c["artist"] and c["artist"].casefold() not in preferred_artists and per[c["artist"]] >= cap:
            return True
        return any(spent[kind][_bucket(c, kind, q)] + 1 > q.get(_bucket(c, kind, q), 0)
                   for kind, q in quotas.items())

    def take(c):
        nonlocal total
        for kind, q in quotas.items():
            spent[kind][_bucket(c, kind, q)] += 1
        per[c["artist"]] += 1
        taken.add(c["video_id"])
        picked.append(c)
        total += c["duration"] or AVG_TRACK_S

    for kind, quota in quotas.items():          # the floor pass
        for key, want in quota.items():
            if not key:
                continue
            for c in order:
                if spent[kind][key] >= want:
                    break
                if c["video_id"] in taken or _bucket(c, kind, quota) != key or blocked(c):
                    continue
                take(c)
    for c in order:                             # then everything else, ceilings still on
        if c["video_id"] not in taken and not blocked(c):
            take(c)
    return picked, total


def _feat(item):
    return {"artist": item.get("artist") or "", "genre": item.get("genre") or "",
            "source": item.get("source") or "theirs"}


def _is_overlap(candidate, state):
    """Whether one of the user's tracks also matches the passengers' explicit taste inputs."""
    inputs = state.get("inputs") or {}
    artists = {a.strip().lower() for a in inputs.get("artists", []) if a}
    if (candidate.get("artist") or "").strip().lower() in artists:
        return True
    wanted = {genre_map.family(g) for g in inputs.get("genres", []) if g}
    return bool(wanted and genre_map.family(candidate.get("genre")) in wanted)


def _mix_source(candidate, state):
    """The three visible pools; shared library tracks still use the library's picking rules."""
    if candidate.get("source") != "mine":
        return "theirs"
    return "blend" if _is_overlap(candidate, state) else "yours"


def _mix_weights(state):
    blend = 1 / 3 if state.get("blend_available") else 0
    own = state.get("own_pct", 50) / 100
    return {"yours": (1 - blend) * own, "theirs": (1 - blend) * (1 - own), "blend": blend}


def _pick_mix(state, groups, orders, target_s):
    """Reserve whole-song shares for all three pools, then fit the trip's duration.

    Estimate the total song count, draw that many in the requested proportions, and refine the
    count using the chosen tracks' actual lengths. Separate minute budgets would give a pool of
    long songs fewer slots than its displayed percentage. Keep Blend separate during shortfall
    replacement too, so a thin passenger pool doesn't erase the shared reservation.
    """
    weights = _mix_weights(state)
    artists = {a.casefold() for a in state.get("inputs", {}).get("artists", [])}
    orders["theirs"] = _prefer_seed_artists(orders["theirs"], artists)
    averages = {side: (sum(c["duration"] or AVG_TRACK_S for c in rows) / len(rows)
                       if rows else AVG_TRACK_S) for side, rows in groups.items()}

    def fill(side, count):
        party = "theirs" if side == "theirs" else "mine"
        quotas = _quotas(state, party, count, groups[side],
                         conditional=party == "mine" and state["blend_available"])
        return _fill(orders[side], count, _artist_cap(groups[side], count), quotas,
                     artists if side == "theirs" else ())

    attempts = {}

    def draw(count):
        if count in attempts:
            return attempts[count]
        wanted = _apportion_percentages(weights, count)
        picked, seconds, short = {}, {}, {}
        expected_s = 0
        for side, slots in wanted.items():
            picked[side], seconds[side] = fill(side, slots)
            avg = seconds[side] / len(picked[side]) if picked[side] else averages[side]
            missing_s = (slots - len(picked[side])) * avg
            expected_s += seconds[side] + missing_s
            if missing_s:
                short["mine" if side == "yours" else side] = round(missing_s / 60)
        if not state.get("building"):
            # Cover missing slots with the outer pools first. Only expand Blend when neither can
            # supply them; its reserved third remains intact when either outer pool runs short.
            gap = count - sum(map(len, picked.values()))
            for side in ("yours", "theirs", "blend"):
                if not gap:
                    break
                replacement, secs = fill(side, len(picked[side]) + gap)
                added = len(replacement) - len(picked[side])
                if added > 0:
                    picked[side], seconds[side] = replacement, secs
                    gap -= added
        secs = sum(seconds.values())
        # While discovery is running, include its still-empty slots in the duration estimate.
        # Otherwise every progress refresh would temporarily replace them with library songs.
        fitted_s = expected_s if state.get("building") else secs
        attempts[count] = (picked, short, fitted_s)
        return attempts[count]

    avg = sum(weights[side] * averages[side] for side in weights)
    count = max(1, round(target_s / max(avg, 60)))
    capacity = max(count, sum(map(len, groups.values())))
    for _ in range(8):
        picked, _, secs = draw(count)
        if not secs:
            break
        next_count = max(1, min(capacity, round(count * target_s / secs)))
        if next_count in attempts:
            break
        count = next_count
    best = min(attempts, key=lambda n: abs(attempts[n][2] - target_s))
    for count in (best - 1, best + 1):
        if 1 <= count <= capacity:
            draw(count)
    picked, short, _ = min(attempts.values(), key=lambda attempt: abs(attempt[2] - target_s))
    return picked, short


def repick(state, store, now=0.0):
    """Re-draw the whole playlist under the state's current mix, familiarity, genre/era quotas and
    crossed-out slots. Mutates and returns `state` (picked, stats, axes).

    Their side comes from the draft's cached pool - it cost network time to assemble. Your side is
    read fresh from the library every time (own_candidates, ~30ms of local SQL): the collection is
    the pool, so there is nothing to cache and nothing to run out of. No network either way, so this
    stays instant."""
    _repair_artist_clusters(state)
    _normalize_balances(state)
    pool = own_candidates(store, now, state) + [c for c in state["pool"] if c["source"] != "mine"]
    rng = random.Random(state["seed"])
    banned = set(state["banned"])
    penalized = set(state.get("prev") or [])
    fam = state["familiarity_pct"] / 100.0
    target_s = state["target_minutes"] * 60
    groups = {"yours": [], "theirs": [], "blend": []}
    for candidate in pool:
        if candidate["video_id"] not in banned:
            groups[_mix_source(candidate, state)].append(candidate)
    state["blend_available"] = bool(groups["blend"])
    orders = {side: _sample_order(cands, rng, lambda c: _weight(c, fam, penalized))
              for side, cands in groups.items()}
    picked, short = _pick_mix(state, groups, orders, target_s)
    mine, theirs = picked["yours"] + picked["blend"], picked["theirs"]
    ordered = journey_order(mine + theirs, "road_trip", state["seed"], _feat)
    # The chosen rows are stored in full, not as ids into a pool: your side isn't kept anywhere, and
    # rendering the playlist (or saving it to YouTube) shouldn't have to reconstruct it.
    state["picked"] = [{k: c[k] for k in _ROW_FIELDS} for c in ordered]
    state["picks"] = [c["video_id"] for c in ordered]
    state["stats"] = {"short": short}
    state["mix_version"] = 2
    _restat(state)
    state["axes"] = {"mine": _merge_axes(state.get("axes", {}).get("mine"), mine,
                                         groups["yours"] + groups["blend"]),
                     "theirs": _merge_axes(state.get("axes", {}).get("theirs"), theirs,
                                           groups["theirs"])}
    for party, axes in state["axes"].items():        # carry each slider's pinned request, if any
        targets = (state.get("targets") or {}).get(party, {})
        for a in axes:
            a["target"] = targets.get(a["key"])
            if party == "theirs" and a["kind"] == "genre":
                a["artists"] = list(_genre_contexts(state, a["name"]))
    # Untagged tracks can't sit on any slider; the panel says so rather than showing an empty column.
    state["untagged"] = {"mine": sum(1 for c in mine if not _axis_genre(c)),
                         "theirs": sum(1 for c in theirs if not _axis_genre(c))}
    _normalize_balances(state)
    return state


def reroll_slot(state, store, index, now=0.0):
    """Cross out one slot: ban that track for this draft and fill the hole, leaving every other slot
    exactly where it is. Repeated crossings keep giving different replacements (the rng is seeded by
    how many have been crossed out so far)."""
    rows = state["picked"]
    if not (0 <= index < len(rows)):
        return state
    gone = rows[index]
    state["banned"] = sorted(set(state["banned"]) | {gone["video_id"]})
    banned = set(state["banned"])
    used = {r["video_id"] for r in rows}
    # Replace like with like, so crossing out a track doesn't quietly shift the mine/theirs balance.
    side = "mine" if gone.get("source") == "mine" else "theirs"
    pool = (own_candidates(store, now, state) if side == "mine"
            else [c for c in state["pool"] if c["source"] != "mine"])
    # ...and, where a slider is pinned, from the same genre/decade, so one swap can't breach a quota.
    quotas = _quota_weights(state, side, [])
    cands = [c for c in pool
             if c["video_id"] not in used and c["video_id"] not in banned
             and _mix_source(c, state) == _mix_source(gone, state)
             and all(_bucket(c, kind, q) == _bucket(gone, kind, q) for kind, q in quotas.items())]
    rng = random.Random(state["seed"] + len(state["banned"]))
    fam = state["familiarity_pct"] / 100.0
    penalized = set(state.get("prev") or [])
    order = _sample_order(cands, rng, lambda c: _weight(c, fam, penalized))
    if side == "theirs":
        artists = {a.casefold() for a in state.get("inputs", {}).get("artists", [])}
        was_named = gone["artist"].casefold() in artists
        same_group = [c for c in order if (c["artist"].casefold() in artists) == was_named]
        order = same_group or order
    if order:
        rows[index] = {k: order[0][k] for k in _ROW_FIELDS}
    else:
        rows.pop(index)                       # nothing left to offer: the slot just goes away
    state["picks"] = [r["video_id"] for r in rows]
    _restat(state)
    return state


def _restat(state):
    """Recompute the counts/lengths after a slot-level edit (no re-draw)."""
    picked = draft_tracks(state)
    for candidate in picked:
        candidate["mix_source"] = _mix_source(candidate, state)
    mine = [c for c in picked if c["source"] == "mine"]
    theirs = [c for c in picked if c["source"] != "mine"]
    yours = [c for c in picked if c["mix_source"] == "yours"]
    blend = [c for c in picked if c["mix_source"] == "blend"]
    state.setdefault("blend_available", bool(blend))

    def mins(rows):
        return round(sum((c["duration"] or AVG_TRACK_S) for c in rows) / 60)

    state["stats"] = {**state.get("stats", {}), "minutes": mins(picked),
                      "own_count": len(mine), "their_count": len(theirs),
                      "own_minutes": mins(mine), "their_minutes": mins(theirs),
                      "yours_count": len(yours), "blend_count": len(blend),
                      "yours_minutes": mins(yours), "blend_minutes": mins(blend),
                      "overlap_count": len(blend),
                      "mix_targets": _apportion_percentages(_mix_weights(state), len(picked))}


def _merge_axes(previous, cands, available=(), max_genres=GENRE_BARS):
    """The genre and era sliders for one party, derived from the tracks currently in the playlist.
    `available` is that side's whole pool, which decides whether a bar is still worth showing.

    Axes are STICKY: a slider you slid to 0 stays on screen with a 0% share, or you would have no way
    to bring that genre back. But sticky is not forever - a bar with nothing left in the POOL to
    match it is dead weight (it happens when the pool is re-drawn, or when a track's genre gets
    re-tagged), so those are dropped unless you have pinned them. New genres are capped at the
    biggest few: specific genres are plentiful and a wall of 1% bars is not a control panel."""
    total = len(cands) or 1
    counts = Counter(_axis_genre(c) for c in cands if _axis_genre(c))
    eras = Counter(c["decade"] for c in cands if c["decade"])
    shares = {("genre:" + n): c / total for n, c in counts.items()}
    shares.update({("era:" + n): c / total for n, c in eras.items()})
    live = {("genre:" + _axis_genre(c)) for c in available if _axis_genre(c)}
    live |= {("era:" + c["decade"]) for c in available if c["decade"]}
    out, seen = [], set()
    for axis in (previous or []):                       # existing sliders keep their place on screen
        if axis["key"] not in live and axis.get("target") is None:
            continue                                    # nothing in the pool can ever fill it again
        seen.add(axis["key"])
        out.append({**axis, "share": round(shares.get(axis["key"], 0.0), 3)})
    room = max(0, max_genres - sum(1 for a in out if a["kind"] == "genre"))
    genres_new = sorted(((k, s) for k, s in shares.items()
                         if k not in seen and k.startswith("genre:")), key=lambda kv: -kv[1])[:room]
    eras_new = sorted((k, s) for k, s in shares.items()
                      if k not in seen and k.startswith("era:"))
    for key, share in genres_new + eras_new:
        kind, name = key.split(":", 1)
        out.append({"key": key, "kind": kind, "name": name, "share": round(share, 3)})
    return order_axes(out)


def order_axes(axes):
    """Genres first, in their sticky order (a bar that moves while you're reaching for it is worse
    than an arbitrary order), then the decades as a number line.

    Applied both when axes are rebuilt and when a stored draft is loaded: the panel renders the axes
    as SAVED, so a draft written before this rule existed would otherwise keep its old order until
    something happened to re-pick it - a server restart and a refresh wouldn't touch it."""
    return ([a for a in axes if a.get("kind") != "era"]
            + sorted((a for a in axes if a.get("kind") == "era"), key=lambda a: a.get("name") or ""))


# --------------------------------------------------------------------------- build / view

def start_draft(store, recipe, now, seed, previous=None):
    """Open a draft with YOUR half already picked. Reads only the library, so it returns in
    milliseconds and the playlist is on screen the instant the button is pressed; their half is
    filled in afterwards, chunk by chunk, by add_other_input. `previous` is the video_id list of the
    last build, penalized here so running the same recipe again gives a different playlist."""
    _, other_size = _pool_targets(recipe)
    pending = ([{"kind": "artist", "name": a} for a in (recipe["artists"] or []) if a]
               + [{"kind": "genre", "name": g} for g in (recipe["genres"] or []) if g])
    # Their side is the only thing the draft holds a pool for; yours is read from the library on
    # every re-pick (own_candidates), so there is nothing to assemble here.
    bare = sum(1 for c in own_candidates(store, now) if not (c["genre"] and c["year"]))
    state = {"recipe_id": recipe["id"], "name": recipe["name"], "seed": seed,
             "own_pct": recipe["own_pct"],
             "blacklist_genres": list(recipe.get("blacklist_genres") or []),
             "familiarity_pct": recipe.get("familiarity_pct", 50),
             "target_minutes": recipe["target_minutes"], "pool": [], "picks": [],
             "picked": [], "banned": [], "own_facts": {},
             "targets": {"mine": {}, "theirs": {}}, "axes": {"mine": [], "theirs": []},
             "prev": list(previous or []), "stats": {}, "saved_playlist_id": None,
             # Two background phases follow: their tracks, then tagging yours. Either alone is
             # enough to keep the panel polling.
             "building": bool(pending) or bare > 0,
             "phase": "theirs" if pending else "mine",
             "pending": pending, "done_inputs": [], "own_facts_left": bare,
             "other_limit": other_size, "other_cap": other_cap(recipe["artists"], recipe["genres"],
                                                               other_size),
             # What the recipe looked like when this draft was drawn, so a later edit can tell what
             # actually changed (apply_recipe) instead of rebuilding from scratch.
             "inputs": {"artists": list(recipe["artists"] or []),
                        "genres": list(recipe["genres"] or []),
                        }}
    return repick(state, store, now)


def add_other_input(state, store, client, item):
    """Fold ONE of their inputs (`{kind, name}`) into an in-progress draft: fetch that artist's or
    genre's tracks, enrich them, merge into the pool and re-pick. Their side grows in front of the
    user instead of the whole page waiting on the slowest lookup.

    A thin artist queues its related artists as further inputs, so widening happens incrementally
    too, and only when it's needed. Only artists the user actually named do that queuing - widening
    from an already-widened artist would wander off into a different taste entirely."""
    name, kind = item["name"], item["kind"]
    # A pin on an era makes the fetch era-aware: "who released alternative rock in the 2000s" is a
    # question MusicBrainz can answer, and a far better search than hoping the decade shows up.
    want = item.get("want") or ""
    decade = want.split(":", 1)[1] if want.startswith("era:") else item.get("decade")
    fresh, related = None, []
    if kind == "genre":
        genre = want.split(":", 1)[1] if want.startswith("genre:") else name
        if decade:
            genre = genre.removesuffix(f" {decade}s")
        fresh = _cluster_candidates(state, store, client, genre, state["other_cap"])
    known = {c["video_id"] for c in state["pool"]}
    theirs = [c for c in state["pool"] if c["source"] != "mine"]
    room = max(0, state["other_limit"] - len(theirs))
    # A track can be in your library AND on their artist's page; it is already yours, so their side
    # doesn't get to claim it twice.
    if fresh is None:
        rows, related = other_input_songs(client, kind, name, state["other_cap"], store, decade)
        for row in rows:
            row["input"] = name        # so removing that artist/genre can take its tracks with it
        fresh = to_candidates([r for r in rows if r["video_id"] not in known][:room], store)
        if kind == "genre":
            origin = _genre_input(state, genre)
            for candidate in fresh:
                candidate["genre_input"] = origin or genre
    else:
        fresh = fresh[:room]
    if kind == "artist" and name in state["inputs"]["artists"]:
        _remember_artist_context(state, name, fresh + theirs, related)
        for candidate in fresh:
            candidate.update(cluster_genre=candidate["genre"], anchors=[name])
    elif item.get("anchor"):
        context = state.get("artist_contexts", {}).get(item["anchor"])
        if context:
            family = genre_map.family(context["genre"])
            fresh = [c for c in fresh if _cluster_anchors(c, {item["anchor"]: context})
                     and (not c["genre"] or c["genre"] == context["genre"]
                          or (family and c["family"] == family))]
            for candidate in fresh:
                candidate.update(cluster_genre=context["genre"], anchors=[item["anchor"]])
    if item.get("want"):
        # This search was run to feed one pinned slider: keep only what actually belongs on it, or
        # widening for "alternative rock" would quietly stuff the mix with whatever else ranked.
        fresh = [c for c in fresh if _matches_axis(c, item["want"])]
    _resolve_durations(store, client, fresh, cap=2)
    state["pool"] = [c for c in state["pool"] if c["source"] == "mine"] + rank_other(theirs + fresh)
    state["done_inputs"].append(name)
    if item.get("widen_key"):
        state.setdefault("done_widening", []).append(item["widen_key"])
    if related and not item.get("related") and len(theirs) + len(fresh) < state["other_limit"]:
        queued = {p["name"] for p in state["pending"]} | set(state["done_inputs"])
        state["pending"] += [{"kind": "artist", "name": n, "related": True, "anchor": name}
                             for n in related if n not in queued]
    return repick(state, store)


def apply_recipe(state, store, now, recipe):
    """Re-point a live draft at an edited recipe, WITHOUT starting over. The form stays usable while
    a playlist is on screen, so edits have to land on the mix you're looking at:

      · an artist or genre you removed  -> their tracks leave the pool immediately
      · one you added                   -> queued, and streamed in by the background worker
      · length / mix / familiarity      -> a re-pick, no network

    Returns the state; `state["pending"]` says whether the caller needs to run the worker again."""
    dropped = ({"artist:" + a for a in recipe["artists"]} | {"genre:" + g for g in recipe["genres"]})
    was = {"artist:" + a for a in state.get("inputs", {}).get("artists", [])} | \
          {"genre:" + g for g in state.get("inputs", {}).get("genres", [])}
    gone = {i.split(":", 1)[1] for i in was - dropped}
    removed_genres = ({_canon_genre(g) for g in state.get("inputs", {}).get("genres", [])}
                      - {_canon_genre(g) for g in recipe["genres"]})
    if set(recipe["artists"]) != set(state.get("inputs", {}).get("artists", [])):
        artists = set(recipe["artists"])
        genres = set(recipe["genres"]) | set(state.get("inputs", {}).get("genres", []))
        state["artist_contexts"] = {a: c for a, c in state.get("artist_contexts", {}).items()
                                    if a in artists}
        # Genre pools depend on the seed artists too. Rebuild those neighborhoods after a seed
        # edit, and remove related tracks whose originating artist is no longer selected.
        state["pool"] = [c for c in state["pool"] if c.get("input") not in genres
                         and (not c.get("anchors") or artists.intersection(c["anchors"]))]
        state["done_inputs"] = [n for n in state["done_inputs"] if n not in genres]
        state["done_widening"] = []
        state["pending"] = [p for p in state["pending"] if p["name"] not in genres
                            and (not p.get("anchor") or p["anchor"] in artists)]
    if gone:      # their tracks came in per input, so they can leave the same way
        keep = {c["video_id"] for c in state["pool"]
                if c["source"] != "mine" and c.get("input") not in gone
                and c.get("genre_input") not in gone}
        state["pool"] = [c for c in state["pool"]
                         if c["source"] == "mine" or c["video_id"] in keep]
        state["done_inputs"] = [n for n in state["done_inputs"] if n not in gone]
        state["pending"] = [p for p in state["pending"] if p["name"] not in gone]
    queued = set(state["done_inputs"]) | {p["name"] for p in state["pending"]}
    state["pending"] += [{"kind": k, "name": n}
                         for k, names in (("artist", recipe["artists"]), ("genre", recipe["genres"]))
                         for n in names if n and n not in queued]
    state["name"] = recipe["name"]
    state["target_minutes"] = recipe["target_minutes"]
    state["own_pct"] = recipe["own_pct"]
    state["blacklist_genres"] = list(recipe.get("blacklist_genres") or [])
    state["familiarity_pct"] = recipe.get("familiarity_pct", state["familiarity_pct"])
    _, other_size = _pool_targets(recipe)
    state["other_cap"] = other_cap(recipe["artists"], recipe["genres"], other_size)
    # A full existing pool must still leave room for the newly selected inputs.
    state["other_limit"] = max(other_size, sum(c["source"] == "theirs" for c in state["pool"])
                               + len(state["pending"]) * state["other_cap"])
    state["inputs"] = {"artists": list(recipe["artists"]), "genres": list(recipe["genres"])}
    for genre in removed_genres:
        axis = "genre:" + genre
        if not _genre_contexts(state, genre) and not _axis_seconds(state, "theirs", axis):
            state["targets"]["theirs"].pop(axis, None)
            state["axes"]["theirs"] = [a for a in state["axes"]["theirs"] if a["key"] != axis]
    _repair_artist_clusters(state)
    _add_genre_targets(state, recipe["genres"])
    if state["pending"]:
        state["building"] = True
        state["phase"] = "theirs"
    return repick(state, store, now)


def fill_own_facts(state, store, now=0.0, count=OWN_FACT_LOOKUPS):
    """Give up to `count` of YOUR still-untagged songs a genre and year from Deezer, so your half
    gets real bars instead of an empty column. An untagged library is the normal case (enrichment is
    incremental and lags), and without this your side is unsteerable however good the mix is.

    Most-listened first: those are the songs the sliders are most likely to reach for, and a lookup
    spent on a song you have never played buys nothing. What it learns is kept on the draft
    (`own_facts`, keyed by video_id) rather than written back to the library - the enrichment
    providers own that column, and an album-level genre from Deezer is not what they would write."""
    learned = state.setdefault("own_facts", {})
    bare = [c for c in own_candidates(store, now, state) if not (c["genre"] and c["year"])]
    bare.sort(key=lambda c: -c["engagement"])
    for c in bare[:count]:
        facts = _facts(c["title"], c["artist"])
        got = {"genre": c["genre"] or facts.get("genre") or "",
               "year": c["year"] or facts.get("year")}
        if got["genre"] or got["year"]:
            learned[c["video_id"]] = got
    state["own_facts_done"] = state.get("own_facts_done", 0) + len(bare[:count])
    state["own_facts_left"] = max(0, len(bare) - count)
    return repick(state, store, now)


def finish_draft(state, store, now=0.0):
    """Both background phases are done: the draft stops being provisional. The final re-pick is the
    first one allowed to cover a short side from the other, now that "short" is really true."""
    state["building"] = False
    state["phase"] = None
    state["pending"] = []
    return repick(state, store, now)


def build_draft(store, client, recipe, now, seed, previous=None):
    """Assemble a whole draft in one blocking call. The incremental path (start_draft +
    add_other_input + finish_draft) is what the web route uses; this is its synchronous equivalent,
    for callers with nothing to show progress to."""
    state = start_draft(store, recipe, now, seed, previous)
    while state["pending"]:
        add_other_input(state, store, client, state["pending"].pop(0))
    fill_own_facts(state, store, now)
    return finish_draft(state, store, now)


def normalized(state, store=None, now=0.0):
    """Bring a stored draft up to the current shape. A draft is persisted JSON, so one written by an
    earlier build can outlive the code that wrote it (the page reopens the last draft); every field
    added since then has to arrive with a default rather than as a missing key the template blows up
    on. Mutates and returns `state`. With a store, repair stale artist clusters and re-pick from
    the corrected pool so reopening and saving an old draft use the same tracks."""
    # Your side used to live in the pool alongside theirs; it is read from the library now, so an
    # older draft's copy is stale weight. Drop it and let the next re-pick supply the real thing.
    state["pool"] = [c for c in state.get("pool") or [] if c.get("source") != "mine"]
    for candidate in state["pool"]:
        candidate.setdefault("cluster_genre", "")
    state.setdefault("own_facts", {})
    for key, default in (("phase", None), ("pending", []), ("done_inputs", []), ("banned", []),
                         ("prev", []), ("building", False), ("build_error", None),
                         ("saved_playlist_id", None), ("own_facts_left", 0), ("own_facts_done", 0),
                         ("familiarity_pct", 50), ("other_limit", MAX_OTHER_POOL),
                         ("other_cap", MIN_ARTIST_CAP)):
        state.setdefault(key, default)
    state.setdefault("targets", {})
    state.setdefault("untagged", {})
    for party in ("mine", "theirs"):
        state["targets"].setdefault(party, {})
        for axis in state.setdefault("axes", {}).setdefault(party, []):
            axis.setdefault("target", state["targets"][party].get(axis.get("key")))
            axis.setdefault("share", 0.0)
        # The panel renders the axes as stored, so ordering has to be applied on the way in as well
        # as on the way out - otherwise an existing draft keeps whatever order it was written with.
        state["axes"][party] = order_axes(state["axes"][party])
    repaired = _repair_artist_clusters(state)
    # Existing unsaved drafts otherwise retain the old duration-based, underfilled Blend draw
    # indefinitely. Reopening repairs it locally; a playlist already saved to YouTube stays put.
    old_mix = state.get("picked") and state.get("mix_version") != 2 and not state["saved_playlist_id"]
    if store is not None and (repaired or old_mix):
        return repick(state, store, now)
    if repaired:
        # Without a library handle, keep the existing order and repair its passenger rows too.
        pool = {c["video_id"]: c for c in state["pool"]}
        state["picked"] = [({**c, "cluster_genre": pool[c["video_id"]]["cluster_genre"]}
                            if c["source"] == "theirs" else c)
                           for c in state.get("picked", [])
                           if c["source"] != "theirs" or c["video_id"] in pool]
        state["picks"] = [c["video_id"] for c in state["picked"]]
        _restat(state)
        state["axes"]["theirs"] = _merge_axes(state["axes"]["theirs"],
                                             [c for c in state["picked"] if c["source"] == "theirs"],
                                             state["pool"])
    for axis in state["axes"]["theirs"]:
        if axis["kind"] == "genre":
            axis["artists"] = list(_genre_contexts(state, axis["name"]))
    for key in ("minutes", "own_count", "their_count", "own_minutes", "their_minutes"):
        state.setdefault("stats", {}).setdefault(key, 0)
    state["stats"].setdefault("short", {})
    _restat(state)
    _normalize_balances(state)
    return state


def draft_tracks(state):
    """The playlist as ordered track dicts, ready for the template and for
    executor.create_generated_playlist (which wants video_id/title/artist/album/thumbnail/duration).
    Stored in the draft as full rows, so this needs no pool and no store."""
    return list(state.get("picked") or [])


def _matches_axis(cand, axis):
    kind, name = axis.split(":", 1)
    return _axis_genre(cand) == name if kind == "genre" else cand["decade"] == name


def _axis_seconds(state, party, axis):
    """How many seconds of THEIR pool match the axis - the ceiling on what a slider can deliver
    without fetching more. Only meaningful for their side; yours is the whole library."""
    return sum((c["duration"] or AVG_TRACK_S) for c in state["pool"]
               if c["source"] != "mine" and _matches_axis(c, axis))


def _widen_terms(state, axis):
    """YouTube searches that would deepen the pool for a pinned axis. A genre bar is named after a
    genre FAMILY, so it expands to that family's member genres; an era bar has no search term of its
    own, so it is crossed with the genres already in play."""
    kind, name = axis.split(":", 1)
    if kind == "genre":
        subs = [s for s in genre_map.subgenres_of(name)][:3]
        return subs or [name]
    genres = [g for g in (state.get("inputs") or {}).get("genres") or []]
    genres += [_axis_genre(c) for c in state["pool"] if c["source"] == "theirs"]
    seen, terms = set(), []
    for g in genres:
        if g and g not in seen:
            seen.add(g)
            terms.append(f"{g} {name}s")
    return terms[:3] or [f"{name}s music"]


def _apportion_percentages(weights, total=100):
    """Scale a group together and allocate rounding leftovers without losing percentage points."""
    if not weights:
        return {}
    weight_sum = sum(weights.values())
    raw = {k: total * (v / weight_sum if weight_sum else 1 / len(weights))
           for k, v in weights.items()}
    allocated = {k: math.floor(v) for k, v in raw.items()}
    remaining = total - sum(allocated.values())
    for key in sorted(raw, key=lambda k: -(raw[k] - allocated[k]))[:remaining]:
        allocated[key] += 1
    return allocated


def _normalize_balances(state):
    """Give every displayed group one coherent budget, including old and partially pinned drafts.

    Targets and observed shares must never be added as independent percentages. Keep valid pins,
    give unpinned rows only the remaining budget, and scale oversubscribed legacy pins together.
    Observed track shares stay separate; balance_pct is the displayed/requested slider position.
    """
    for party in ("mine", "theirs"):
        axes = state.setdefault("axes", {}).setdefault(party, [])
        targets = state.setdefault("targets", {}).setdefault(party, {})
        for kind in ("genre", "era"):
            rows = [a for a in axes if a["kind"] == kind]
            keys = {a["key"] for a in rows}
            for key in targets:
                if key.startswith(kind + ":") and key not in keys:
                    row = {"key": key, "kind": kind, "name": key.split(":", 1)[1], "share": 0.0}
                    axes.append(row)
                    rows.append(row)
            if not rows:
                continue
            pins = {a["key"]: targets[a["key"]] for a in rows if a["key"] in targets}
            floating = {a["key"]: a.get("share", 0.0) for a in rows if a["key"] not in pins}
            if pins:
                # A lone genre can still be turned down/off when there is no sibling to receive
                # its remainder. Multi-slider groups always share the full 100% budget.
                if len(rows) == 1:
                    amounts = {rows[0]["key"]: round(next(iter(pins.values())) * 100)}
                elif sum(pins.values()) >= 1 or not floating:
                    amounts = {k: 0 for k in floating}
                    amounts.update(_apportion_percentages(pins))
                else:
                    claimed = round(sum(pins.values()) * 100)
                    amounts = _apportion_percentages(pins, claimed)
                    amounts.update(_apportion_percentages(floating, 100 - claimed))
                targets.update({k: amounts[k] / 100 for k in pins})
            else:
                amounts = _apportion_percentages(floating)
            for row in rows:
                row["balance_pct"] = amounts[row["key"]]
                row["target"] = targets.get(row["key"])
        state["axes"][party] = order_axes(axes)


def _add_genre_targets(state, genres):
    """Show selected passenger genres before discovery, each with 1 / the new genre count.

    Reserve all additions together, then scale the existing balance into the remainder. Pins
    keep these empty rows visible while the worker fetches tracks and preserve the requested mix.
    """
    _normalize_balances(state)
    axes = state["axes"]["theirs"]
    existing = {a["key"]: a["balance_pct"] for a in axes if a["kind"] == "genre"}
    added = {"genre:" + name: name for genre in genres if (name := _canon_genre(genre))
             and "genre:" + name not in existing}
    if not added:
        return
    reserved = round(100 * len(added) / (len(existing) + len(added)))
    amounts = _apportion_percentages(existing, 100 - reserved)
    amounts.update(_apportion_percentages({key: 1 for key in added}, reserved))
    state["targets"]["theirs"].update({key: pct / 100 for key, pct in amounts.items()})
    axes.extend({"key": key, "kind": "genre", "name": name, "share": 0.0,
                 "target": amounts[key] / 100} for key, name in added.items())


def _rebalance_targets(state, party, axis, share):
    """Keep the edited percentage and apportion the remainder among its sibling sliders.

    Largest-remainder rounding keeps displayed whole percentages at exactly 100. If all siblings
    were at zero (after a 100% selection), spread the remainder evenly so they can come back.
    Genres and years, and the two parties, are independent groups.
    """
    kind, name = axis.split(":", 1)
    targets = state.setdefault("targets", {}).setdefault(party, {})
    axes = state.setdefault("axes", {}).setdefault(party, [])
    if not any(a["key"] == axis for a in axes):
        axes.append({"key": axis, "kind": kind, "name": name, "share": 0.0, "target": share})
    siblings = {a["key"]: targets.get(a["key"], a.get("balance_pct", a.get("share", 0.0) * 100) / 100)
                for a in axes if a["kind"] == kind and a["key"] != axis}
    siblings.update({k: v for k, v in targets.items()
                     if k.startswith(kind + ":") and k != axis})
    pct = round(share * 100)
    targets[axis] = pct / 100
    if not siblings:
        return
    allocated = _apportion_percentages(siblings, 100 - pct)
    targets.update({k: v / 100 for k, v in allocated.items()})


def set_share(state, party, axis, share, store, now=0.0):
    """Set a requested share and proportionally rebalance the other genres or years on that side.

    Your side needs no widening: it IS your whole collection, so whatever rock you own is already in
    play. Theirs is finite and bought over the network, so a request bigger than their pool queues a
    YouTube search, run in the background by the caller (state["pending"]), classified on arrival and
    filtered to the axis so a loose search can't pollute the mix."""
    if party not in ("mine", "theirs") or not axis.startswith(("genre:", "era:")):
        return state
    share = max(0.0, min(1.0, float(share)))
    _rebalance_targets(state, party, axis, share)
    if party == "theirs":
        budget = state["target_minutes"] * 60 * (100 - state["own_pct"]) / 100.0
        prefix = axis.split(":", 1)[0] + ":"
        for key, target in state["targets"][party].items():
            if not key.startswith(prefix) or target <= 0:
                continue
            needs_context = (key.startswith("genre:") and state["inputs"].get("artists")
                             and any(not c.get("cluster_genre") for c in state["pool"]
                                     if _matches_axis(c, key)))
            if not needs_context and _axis_seconds(state, party, key) >= target * budget:
                continue
            queued = set(state.get("done_widening", [])) | {p.get("widen_key") for p in state["pending"]}
            fresh = []
            for term in _widen_terms(state, key):
                widen_key = f"{key}:{target}:{budget}:{term}"
                if widen_key not in queued:
                    fresh.append({"kind": "genre", "name": term, "want": key,
                                  "related": True, "widen_key": widen_key})
            # Room for what the search brings back, or it arrives and is trimmed straight off.
            state["other_limit"] += len(fresh) * state["other_cap"]
            state["pending"] += fresh
            if state["pending"]:
                state["building"] = True
                state["phase"] = "theirs"
    return repick(state, store, now)


def clear_share(state, party, axis, store, now=0.0):
    """Release a balanced group so its genres or years follow the available tracks again."""
    targets = (state.get("targets") or {}).get(party, {})
    prefix = axis.split(":", 1)[0] + ":"
    for key in list(targets):
        if key.startswith(prefix):
            del targets[key]
    return repick(state, store, now)
