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
from collections import Counter
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
               "genre", "family", "year", "decade")


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
            "year": year, "decade": _decade(year), "plays": plays, "fam": 0.0, "score": 0.0}


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
    return cand["genre"] or cand["family"] or ""


def _bucket(cand, kind, quota):
    """Which quota bucket a candidate falls in for `kind`: its own pinned axis, else the shared
    remainder (""). A track with no genre/decade at all always lands in the remainder."""
    key = ("genre:" + _axis_genre(cand)) if kind == "genre" else ("era:" + cand["decade"])
    return key if key in quota else ""


def _slots(cands, budget_s):
    """Roughly how many tracks fit this side's budget, using the pool's own average length. The
    sliders are a share of the SONGS in the playlist ("40% of my songs are rock"), so the quotas they
    turn into have to be counts, even though the playlist as a whole is budgeted by duration."""
    durations = [c["duration"] for c in cands if c["duration"]]
    avg = (sum(durations) / len(durations)) if durations else AVG_TRACK_S
    return max(1, round(budget_s / max(avg, 60)))


def _fill_side(order, budget_s, cap, state, party, cands):
    """Fill one side to its duration budget, honouring the genre/era quotas.

    Quotas are counts (the bars are "40% of my songs"), so how many slots the budget buys has to be
    estimated - and an estimate off by half a minute per track leaves the side minutes short, because
    the unpinned bucket's count is also a ceiling. So: fill, and if the songs that actually got picked
    are shorter than the collection's average, re-estimate from THEM and fill again."""
    quotas = _quotas(state, party, budget_s, cands)
    picked, secs, rest = _fill(order, budget_s, cap, quotas)
    for _ in range(2):
        if not quotas or not picked or secs >= budget_s - AVG_TRACK_S / 2:
            break
        slots = max(1, round(budget_s / max(secs / len(picked), 60)))
        quotas = _quotas(state, party, budget_s, cands, slots=slots)
        picked, secs, rest = _fill(order, budget_s, cap, quotas)
    return picked, secs, rest


def _quotas(state, party, budget_s, cands, slots=None):
    """How many of `party`'s tracks each pinned genre/era gets, plus the "" bucket every unpinned
    track shares. Only kinds with at least one pinned slider get a quota; the rest stay free.

    This is what makes a slider mean what it says, in BOTH directions. As a ceiling it holds the
    others back (drag one decade to 100% and the rest empty out). As a floor it is filled first, so
    asking for 40% rock gets 40% rock - weighting alone would just re-order the draw and hand back
    whatever proportion the pool happened to hold, which is not what the number on screen says."""
    targets = (state.get("targets") or {}).get(party) or {}
    out, n = {}, (slots or _slots(cands, budget_s))
    for kind in ("genre", "era"):
        pins = {k: v for k, v in targets.items() if k.startswith(kind + ":")}
        if not pins:
            continue
        claimed = sum(pins.values())
        if claimed > 1.0:                       # over-subscribed sliders: scale them back together
            pins = {k: v / claimed for k, v in pins.items()}
            claimed = 1.0
        out[kind] = {k: (max(1, round(v * n)) if v > 0 else 0) for k, v in pins.items()}
        out[kind][""] = max(0, n - sum(out[kind].values()))
    return out


def _fill(order, budget_s, cap, quotas=None):
    """Take candidates from a sampled order until the duration budget is met, honouring an artist
    cap and any per-genre/era quotas. Returns (picked, seconds, leftovers).

    Pinned buckets are filled FIRST, up to their count - a quota is a floor as much as a ceiling.
    Then the rest of the budget is filled in sampled order, with every ceiling still enforced.

    A track is taken when it lands the running total NEARER the budget than stopping would, so each
    side rounds to its closest whole-track length rather than always overshooting - two sides that
    each overshoot make a playlist noticeably longer than the trip."""
    quotas = quotas or {}
    picked, per, taken = [], Counter(), set()
    spent = {kind: Counter() for kind in quotas}
    total = 0.0

    def blocked(c):
        secs = c["duration"] or AVG_TRACK_S
        if total + secs - budget_s > secs / 2:
            return True
        if c["artist"] and per[c["artist"]] >= cap:
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
    return picked, total, [c for c in order if c["video_id"] not in taken]


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


def repick(state, store, now=0.0):
    """Re-draw the whole playlist under the state's current mix, familiarity, genre/era quotas and
    crossed-out slots. Mutates and returns `state` (picked, stats, axes).

    Their side comes from the draft's cached pool - it cost network time to assemble. Your side is
    read fresh from the library every time (own_candidates, ~30ms of local SQL): the collection is
    the pool, so there is nothing to cache and nothing to run out of. No network either way, so this
    stays instant."""
    pool = own_candidates(store, now, state) + [c for c in state["pool"] if c["source"] != "mine"]
    rng = random.Random(state["seed"])
    banned = set(state["banned"])
    penalized = set(state.get("prev") or [])
    fam = state["familiarity_pct"] / 100.0
    target_s = state["target_minutes"] * 60
    own_all = [c for c in pool if c["source"] == "mine" and c["video_id"] not in banned]
    overlap = [c for c in own_all if _is_overlap(c, state)]
    # When shared taste exists, reserve the quiet middle third for it. The visible slider divides
    # the remaining two thirds between the user's exclusive taste and the passengers' catalogue.
    overlap_budget = target_s / 3.0 if overlap else 0.0
    outer_budget = target_s - overlap_budget
    own_exclusive_budget = outer_budget * state["own_pct"] / 100.0
    own_budget = own_exclusive_budget + overlap_budget

    sides = {}
    shared_ids = set()
    for side, budget in (("mine", own_budget), ("theirs", target_s - own_budget)):
        cands = [c for c in pool if c["source"] == side and c["video_id"] not in banned]
        if side == "mine" and overlap:
            exclusive = [c for c in cands if c not in overlap]
            overlap_order = _sample_order(overlap, rng, lambda c: _weight(c, fam, penalized))
            exclusive_order = _sample_order(exclusive, rng, lambda c: _weight(c, fam, penalized))
            overlap_cap = _artist_cap(overlap, max(1, round(overlap_budget / AVG_TRACK_S)))
            own_cap = _artist_cap(exclusive, max(1, round(own_exclusive_budget / AVG_TRACK_S)))
            shared, shared_s, shared_rest = _fill_side(
                overlap_order, overlap_budget, overlap_cap, state, "mine", overlap)
            shared_ids = {c["video_id"] for c in shared}
            personal, personal_s, personal_rest = _fill_side(
                exclusive_order, own_exclusive_budget, own_cap, state, "mine", exclusive)
            sides[side] = {"picked": personal + shared, "secs": personal_s + shared_s,
                           "rest": personal_rest + shared_rest, "budget": budget,
                           "order": overlap_order + exclusive_order,
                           "cap": max(overlap_cap, own_cap), "cands": cands}
            continue
        order = _sample_order(cands, rng, lambda c: _weight(c, fam, penalized))
        cap = _artist_cap(cands, max(1, round(budget / AVG_TRACK_S)))
        picked, secs, rest = _fill_side(order, budget, cap, state, side, cands)
        sides[side] = {"picked": picked, "secs": secs, "rest": rest, "budget": budget,
                       "order": order, "cap": cap, "cands": cands}

    # One side ran dry (a recipe naming one obscure artist, or a genre slider turned everything off):
    # spend its unmet budget on the other side rather than shipping a short playlist, and report it,
    # so the panel can say the mix isn't what was asked for instead of silently drifting.
    #
    # Not while the build is still running, though: their side is legitimately incomplete then, and
    # covering for it would fill the list with your tracks only to evict them a second later. The
    # slots simply stay empty until their half arrives.
    short = {}
    for side, other in (("mine", "theirs"), ("theirs", "mine")):
        gap = sides[side]["budget"] - sides[side]["secs"]
        if state.get("building") or gap <= AVG_TRACK_S or not sides[other]["rest"]:
            continue
        short[side] = round(gap / 60)
        # Re-fill the other side against the larger budget rather than appending to it, so its own
        # quotas still hold: covering a shortfall must not smuggle back a genre you slid out.
        budget = sides[other]["budget"] + gap
        picked, secs, rest = _fill_side(sides[other]["order"], budget, sides[other]["cap"],
                                        state, other, sides[other]["cands"])
        sides[other].update(picked=picked, secs=secs, rest=rest)

    mine, theirs = sides["mine"]["picked"], sides["theirs"]["picked"]
    ordered = journey_order(mine + theirs, "road_trip", state["seed"], _feat)
    # The chosen rows are stored in full, not as ids into a pool: your side isn't kept anywhere, and
    # rendering the playlist (or saving it to YouTube) shouldn't have to reconstruct it.
    state["picked"] = [{k: c[k] for k in _ROW_FIELDS} for c in ordered]
    state["picks"] = [c["video_id"] for c in ordered]
    total_s = sum((c["duration"] or AVG_TRACK_S) for c in ordered)
    state["stats"] = {"minutes": round(total_s / 60), "own_count": len(mine),
                      "their_count": len(theirs),
                      "overlap_count": sum(1 for c in mine if c["video_id"] in shared_ids),
                      "own_minutes": round(sides["mine"]["secs"] / 60),
                      "their_minutes": round(sides["theirs"]["secs"] / 60),
                      "short": short}
    state["axes"] = {"mine": _merge_axes(state.get("axes", {}).get("mine"), mine,
                                         sides["mine"]["cands"]),
                     "theirs": _merge_axes(state.get("axes", {}).get("theirs"), theirs,
                                           sides["theirs"]["cands"])}
    for party, axes in state["axes"].items():        # carry each slider's pinned request, if any
        targets = (state.get("targets") or {}).get(party, {})
        for a in axes:
            a["target"] = targets.get(a["key"])
    # Untagged tracks can't sit on any slider; the panel says so rather than showing an empty column.
    state["untagged"] = {"mine": sum(1 for c in mine if not _axis_genre(c)),
                         "theirs": sum(1 for c in theirs if not _axis_genre(c))}
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
    quotas = _quotas(state, side, 1.0, [])
    cands = [c for c in pool
             if c["video_id"] not in used and c["video_id"] not in banned
             and all(_bucket(c, kind, q) == _bucket(gone, kind, q) for kind, q in quotas.items())]
    rng = random.Random(state["seed"] + len(state["banned"]))
    fam = state["familiarity_pct"] / 100.0
    penalized = set(state.get("prev") or [])
    order = _sample_order(cands, rng, lambda c: _weight(c, fam, penalized))
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
    mine = [c for c in picked if c["source"] == "mine"]
    theirs = [c for c in picked if c["source"] != "mine"]

    def mins(rows):
        return round(sum((c["duration"] or AVG_TRACK_S) for c in rows) / 60)

    state["stats"] = {**state.get("stats", {}), "minutes": mins(picked),
                      "own_count": len(mine), "their_count": len(theirs),
                      "own_minutes": mins(mine), "their_minutes": mins(theirs)}


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
    rows, related = other_input_songs(client, kind, name, state["other_cap"], store, decade)
    known = {c["video_id"] for c in state["pool"]}
    theirs = [c for c in state["pool"] if c["source"] != "mine"]
    room = max(0, state["other_limit"] - len(theirs))
    # A track can be in your library AND on their artist's page; it is already yours, so their side
    # doesn't get to claim it twice.
    for row in rows:
        row["input"] = name            # so removing that artist/genre can take its tracks with it
    fresh = to_candidates([r for r in rows if r["video_id"] not in known][:room], store)
    if item.get("want"):
        # This search was run to feed one pinned slider: keep only what actually belongs on it, or
        # widening for "alternative rock" would quietly stuff the mix with whatever else ranked.
        fresh = [c for c in fresh if _matches_axis(c, item["want"])]
    _resolve_durations(store, client, fresh, cap=2)
    state["pool"] = [c for c in state["pool"] if c["source"] == "mine"] + rank_other(theirs + fresh)
    state["done_inputs"].append(name)
    if related and not item.get("related") and len(theirs) + len(fresh) < state["other_limit"]:
        queued = {p["name"] for p in state["pending"]} | set(state["done_inputs"])
        state["pending"] += [{"kind": "artist", "name": n, "related": True}
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
    if gone:      # their tracks came in per input, so they can leave the same way
        keep = {c["video_id"] for c in state["pool"]
                if c["source"] != "mine" and c.get("input") not in gone}
        state["pool"] = [c for c in state["pool"]
                         if c["source"] == "mine" or c["video_id"] in keep]
        state["done_inputs"] = [n for n in state["done_inputs"] if n not in gone]
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
    state["other_limit"] = other_size
    state["other_cap"] = other_cap(recipe["artists"], recipe["genres"], other_size)
    state["inputs"] = {"artists": list(recipe["artists"]), "genres": list(recipe["genres"])}
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


def normalized(state):
    """Bring a stored draft up to the current shape. A draft is persisted JSON, so one written by an
    earlier build can outlive the code that wrote it (the page reopens the last draft); every field
    added since then has to arrive with a default rather than as a missing key the template blows up
    on. Mutates and returns `state`."""
    # Your side used to live in the pool alongside theirs; it is read from the library now, so an
    # older draft's copy is stale weight. Drop it and let the next re-pick supply the real thing.
    state["pool"] = [c for c in state.get("pool") or [] if c.get("source") != "mine"]
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
    for key in ("minutes", "own_count", "their_count", "own_minutes", "their_minutes"):
        state.setdefault("stats", {}).setdefault(key, 0)
    state["stats"].setdefault("short", {})
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


def set_share(state, party, axis, share, store, now=0.0):
    """Ask for a genre or era to be `share` (0..1) of that party's tracks, and re-pick under that
    quota. Where the slider sits IS the share that genre has in the playlist below it, so dragging
    it says "make it this much" and everything else gives way (see _quotas).

    Your side needs no widening: it IS your whole collection, so whatever rock you own is already in
    play. Theirs is finite and bought over the network, so a request bigger than their pool queues a
    YouTube search, run in the background by the caller (state["pending"]), classified on arrival and
    filtered to the axis so a loose search can't pollute the mix."""
    if party not in ("mine", "theirs"):
        return state
    share = max(0.0, min(1.0, float(share)))
    state.setdefault("targets", {}).setdefault(party, {})[axis] = share
    if share > 0 and party == "theirs":
        budget = state["target_minutes"] * 60 * (100 - state["own_pct"]) / 100.0
        if _axis_seconds(state, party, axis) < share * budget:
            queued = set(state["done_inputs"]) | {p["name"] for p in state["pending"]}
            fresh = [t for t in _widen_terms(state, axis) if t not in queued]
            # Room for what the search brings back, or it arrives and is trimmed straight off.
            state["other_limit"] += len(fresh) * state["other_cap"]
            state["pending"] += [{"kind": "genre", "name": t, "want": axis, "related": True}
                                 for t in fresh]
            if state["pending"]:
                state["building"] = True
                state["phase"] = "theirs"
    return repick(state, store, now)


def clear_share(state, party, axis, store, now=0.0):
    """Unpin a slider: that genre floats with the rest of the mix again."""
    (state.get("targets") or {}).get(party, {}).pop(axis, None)
    return repick(state, store, now)
