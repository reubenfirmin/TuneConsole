"""Road Trip draft transitions and normalization.

Candidate acquisition belongs to road_trip_candidates; fixed-input allocation and ordering
belong to road_trip_mix. This service owns the JSON draft's build, edit, normalize and reroll
transitions. Routes persist the resulting draft through repos.road_trip and save playlists
through library.executor.

Blend your taste-weighted tracks (the 'own' pool) with popular
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
from collections import Counter
from itertools import zip_longest

from yt_playlist.rec import road_trip_candidates as candidates, road_trip_mix
from yt_playlist.rec.road_trip_candidates import _canon_genre, other_cap, own_candidates
from yt_playlist.rec.road_trip_mix import (_apportion_percentages, _axis_genre, _bucket,
                                        _mix_source, _mix_weights, _quota_weights,
                                        _sample_order, _weight)
from yt_playlist.util import genre_map

MIN_ARTIST_CAP = candidates.MIN_ARTIST_CAP
MAX_OTHER_POOL = 44        # ceiling on their pool: every candidate costs a Deezer lookup
AVG_TRACK_S = road_trip_mix.AVG_TRACK_S
POOL_SLACK = 3.0           # candidates per slot, so sliders/rerolls have somewhere to go
OWN_FACT_LOOKUPS = 45      # cap on Deezer lookups spent tagging your own untagged candidates
GENRE_BARS = 8             # genre sliders shown per party (plus any you've already pinned)
GENRE_ARTISTS = candidates.GENRE_ARTISTS

# What a picked row carries into the draft (and on to YouTube). Everything the panel renders plus
# what the ordering and the bars need, and nothing else - the pool entries also hold scoring scratch.
_ROW_FIELDS = ("video_id", "title", "artist", "album", "thumbnail", "duration", "source",
               "genre", "family", "year", "decade", "cluster_genre")


# --------------------------------------------------------------------------- candidate assembly

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
        page, _ = candidates._artist_page(client, name)
        songs = candidates.to_candidates(candidates._page_songs(page, name, limit=5), store)
        _remember_artist_context(state, name, songs, candidates._related_names(page, limit=GENRE_ARTISTS))


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
        rows = [r for r in candidates._artist_songs(client, name) if r["video_id"] not in known]
        accepted = 0
        for candidate in candidates.to_candidates(rows[:per_artist * 3], store):
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


# --------------------------------------------------------------------------- picking

def _pool_targets(recipe):
    """(own_pool_size, other_pool_size) for a recipe's target length. Both sides are drawn deeper
    than their current share needs, so moving the mix slider after the build still has candidates to
    reach for without a rebuild. Their side is capped harder: every candidate costs a Deezer lookup,
    and the build has to stay interactive."""
    slots = max(8, math.ceil(recipe["target_minutes"] * 60 / AVG_TRACK_S))
    return (min(140, max(16, int(slots * POOL_SLACK))),
            min(MAX_OTHER_POOL, max(12, int(slots * POOL_SLACK * 0.6))))


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
    result = road_trip_mix.construct(road_trip_mix.MixRequest(
        candidates=pool, own_pct=state["own_pct"], familiarity_pct=state["familiarity_pct"],
        target_minutes=state["target_minutes"], seed=state["seed"],
        artists=state.get("inputs", {}).get("artists", []), genres=state.get("inputs", {}).get("genres", []),
        targets=state.get("targets") or {}, banned=state["banned"], previous=state.get("prev") or [],
        building=bool(state.get("building"))),
        random.Random(state["seed"]))
    groups, picked, short = result.groups, result.picked, result.short
    state["blend_available"] = result.blend_available
    mine, theirs = picked["yours"] + picked["blend"], picked["theirs"]
    ordered = result.ordered
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
        batch = candidates.acquire_input(client, store, candidates.InputRequest(kind, name, state["other_cap"], decade))
        rows, related = batch.rows, batch.related
        for row in rows:
            row["input"] = name        # so removing that artist/genre can take its tracks with it
        fresh = candidates.to_candidates([r for r in rows if r["video_id"] not in known][:room], store)
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
    candidates._resolve_durations(store, client, fresh, cap=2)
    state["pool"] = [c for c in state["pool"] if c["source"] == "mine"] + candidates.rank_other(theirs + fresh)
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
        facts = candidates._facts(c["title"], c["artist"])
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
