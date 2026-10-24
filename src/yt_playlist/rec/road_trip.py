"""Road Trip draft transitions and normalization.

Candidate acquisition belongs to road_trip_candidates, artist relationships to
road_trip_neighborhoods, balancing to road_trip_axes and allocation to road_trip_mix. This service
owns transitions over the road_trip_state JSON contract. Routes retain generation/job ownership,
persist through repos.road_trip and save playlists through library.executor.

Blend your taste-weighted tracks (the 'own' pool) with popular
tracks pulled from YouTube for other people's artists/genres (the 'other' pool), mixed to a target
ratio and cut to a target duration. Neither pool needs bespoke taste-model handling: the result is
materialized via playlist_generated.create_generated_playlist with the normal Generated group, which already
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

from yt_playlist.rec import road_trip_axes, road_trip_candidates as candidates, road_trip_mix
from yt_playlist.rec.road_trip_candidates import other_cap, own_candidates
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.rec.road_trip_mix import (_axis_genre, _bucket, _mix_source, _quota_weights,
                                        _sample_order, _weight)
from yt_playlist.rec.road_trip_neighborhoods import ArtistNeighborhoods
from yt_playlist.rec.road_trip_state import (DraftState, MAX_OTHER_POOL, ROW_FIELDS, draft_tracks,
                                          normalize_shape, restat)

AVG_TRACK_S = road_trip_mix.AVG_TRACK_S
POOL_SLACK = 3.0           # candidates per slot, so sliders/rerolls have somewhere to go
OWN_FACT_LOOKUPS = 45      # cap on Deezer lookups spent tagging your own untagged candidates


def _neighborhoods(state: DraftState) -> ArtistNeighborhoods:
    inputs = state.get("inputs", {})
    return ArtistNeighborhoods(inputs.get("artists", []), inputs.get("genres", []),
                               state.setdefault("artist_contexts", {}))


def _repair_artist_clusters(state):
    """Apply neighborhood repair to draft controls and queued work, without acquisition."""
    neighborhoods = _neighborhoods(state)
    contexts = neighborhoods.contexts
    artists = neighborhoods.artists
    if not any(n in contexts for n in artists) or any(n not in contexts or not contexts[n]["genre"] for n in artists):
        return False
    explicit = {canonical_genre(g) for g in neighborhoods.genres}
    repair = neighborhoods.repair(state["pool"])
    state["pool"] = repair.pool
    changed = repair.changed
    live = {"genre:" + _axis_genre(c) for c in repair.pool if c["source"] == "theirs"}
    axes = state.get("axes", {}).get("theirs", [])
    genres = {a["key"]: a["name"] for a in axes if a["kind"] == "genre"}
    genres.update({k: k.split(":", 1)[1] for k in state.get("targets", {}).get("theirs", {})
                   if k.startswith("genre:")})
    obsolete = {key for key, genre in genres.items() if key not in live
                and not neighborhoods.for_genre(genre) and canonical_genre(genre) not in explicit}
    if obsolete:
        state["axes"]["theirs"] = [a for a in axes if a["key"] not in obsolete]
        for key in obsolete:
            state.get("targets", {}).get("theirs", {}).pop(key, None)
        state["pending"] = [p for p in state.get("pending", []) if p.get("want") not in obsolete]
        changed = True
    if changed:
        state["done_widening"] = []  # the previous search may have fetched the wrong neighborhood
        state["done_inputs"] = [n for n in state.get("done_inputs", [])
                                if n not in repair.removed_inputs or n in artists or canonical_genre(n) in explicit]
    return changed


# --------------------------------------------------------------------------- picking

def _pool_targets(recipe):
    """(own_pool_size, other_pool_size) for a recipe's target length. Both sides are drawn deeper
    than their current share needs, so moving the mix slider after the build still has candidates to
    reach for without a rebuild. Their side is capped harder: every candidate costs a Deezer lookup,
    and the build has to stay interactive."""
    slots = max(8, math.ceil(recipe["target_minutes"] * 60 / AVG_TRACK_S))
    return (min(140, max(16, int(slots * POOL_SLACK))),
            min(MAX_OTHER_POOL, max(12, int(slots * POOL_SLACK * 0.6))))


def repick(state: DraftState, store, now=0.0) -> DraftState:
    """Re-draw the whole playlist under the state's current mix, familiarity, genre/era quotas and
    crossed-out slots. Mutates and returns `state` (picked, stats, axes).

    Their side comes from the draft's cached pool - it cost network time to assemble. Your side is
    read fresh from the library every time (own_candidates, ~30ms of local SQL): the collection is
    the pool, so there is nothing to cache and nothing to run out of. No network either way, so this
    stays instant."""
    _repair_artist_clusters(state)
    for party in ("mine", "theirs"):
        state["axes"][party] = road_trip_axes.normalize(state["axes"][party], state["targets"][party])
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
    state["picked"] = [{k: c[k] for k in ROW_FIELDS} for c in ordered]
    state["picks"] = [c["video_id"] for c in ordered]
    state["stats"] = {"short": short}
    state["mix_version"] = 2
    restat(state)
    state["axes"] = {"mine": road_trip_axes.merge(state.get("axes", {}).get("mine"), mine,
                                         groups["yours"] + groups["blend"]),
                     "theirs": road_trip_axes.merge(state.get("axes", {}).get("theirs"), theirs,
                                           groups["theirs"])}
    for party, axes in state["axes"].items():        # carry each slider's pinned request, if any
        targets = (state.get("targets") or {}).get(party, {})
        for a in axes:
            a["target"] = targets.get(a["key"])
            if party == "theirs" and a["kind"] == "genre":
                a["artists"] = list(_neighborhoods(state).for_genre(a["name"]))
    # Untagged tracks can't sit on any slider; the panel says so rather than showing an empty column.
    state["untagged"] = {"mine": sum(1 for c in mine if not _axis_genre(c)),
                         "theirs": sum(1 for c in theirs if not _axis_genre(c))}
    for party in ("mine", "theirs"):
        state["axes"][party] = road_trip_axes.normalize(state["axes"][party], state["targets"][party])
    return state


def reroll_slot(state: DraftState, store, index, now=0.0) -> DraftState:
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
        rows[index] = {k: order[0][k] for k in ROW_FIELDS}
    else:
        rows.pop(index)                       # nothing left to offer: the slot just goes away
    state["picks"] = [r["video_id"] for r in rows]
    restat(state)
    return state


# --------------------------------------------------------------------------- build / view

def start_draft(store, recipe, now, seed, previous=None) -> DraftState:
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
    state: DraftState = {"recipe_id": recipe["id"], "name": recipe["name"], "seed": seed,
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


def add_other_input(state: DraftState, store, client, item) -> DraftState:
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
    neighborhoods = _neighborhoods(state)
    if kind == "genre":
        genre = want.split(":", 1)[1] if want.startswith("genre:") else name
        if decade:
            genre = genre.removesuffix(f" {decade}s")
        cluster = neighborhoods.resolve(state["pool"], store, client, genre, state["other_cap"])
        if cluster is not None:
            state["pool"], fresh = cluster.pool, cluster.fresh
    known = {c["video_id"] for c in state["pool"]}
    theirs = [c for c in state["pool"] if c["source"] != "mine"]
    room = max(0, state["other_limit"] - len(theirs))
    # Cache passenger results independently of the live library. Construction assigns each upload
    # one owner when merging these results with the current library on every re-pick.
    if fresh is None:
        batch = candidates.acquire_input(client, store, candidates.InputRequest(kind, name, state["other_cap"], decade))
        rows, related = batch.rows, batch.related
        for row in rows:
            row["input"] = name        # so removing that artist/genre can take its tracks with it
        fresh = candidates.to_candidates([r for r in rows if r["video_id"] not in known][:room], store)
        if kind == "genre":
            origin = neighborhoods.genre_input(state["pool"], genre)
            for candidate in fresh:
                candidate["genre_input"] = origin or genre
    else:
        fresh = fresh[:room]
    fresh = neighborhoods.attribute_input(name, kind, fresh, theirs, related, item.get("anchor"))
    if item.get("want"):
        # This search was run to feed one pinned slider: keep only what actually belongs on it, or
        # widening for "alternative rock" would quietly stuff the mix with whatever else ranked.
        fresh = [c for c in fresh if road_trip_axes.matches(c, item["want"])]
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


def apply_recipe(state: DraftState, store, now, recipe) -> DraftState:
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
    removed_genres = ({canonical_genre(g) for g in state.get("inputs", {}).get("genres", [])}
                      - {canonical_genre(g) for g in recipe["genres"]})
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
        if not _neighborhoods(state).for_genre(genre) and not road_trip_axes.available_seconds(state["pool"], axis):
            state["targets"]["theirs"].pop(axis, None)
            state["axes"]["theirs"] = [a for a in state["axes"]["theirs"] if a["key"] != axis]
    _repair_artist_clusters(state)
    for party in ("mine", "theirs"):
        state["axes"][party] = road_trip_axes.normalize(state["axes"][party], state["targets"][party])
    road_trip_axes.add_genres(state["axes"]["theirs"], state["targets"]["theirs"], recipe["genres"])
    if state["pending"]:
        state["building"] = True
        state["phase"] = "theirs"
    return repick(state, store, now)


def fill_own_facts(state: DraftState, store, now=0.0, count=OWN_FACT_LOOKUPS) -> DraftState:
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


def finish_draft(state: DraftState, store, now=0.0) -> DraftState:
    """Both background phases are done: the draft stops being provisional. The final re-pick is the
    first one allowed to cover a short side from the other, now that "short" is really true."""
    state["building"] = False
    state["phase"] = None
    state["pending"] = []
    return repick(state, store, now)


def build_draft(store, client, recipe, now, seed, previous=None) -> DraftState:
    """Assemble a whole draft in one blocking call. The incremental path (start_draft +
    add_other_input + finish_draft) is what the web route uses; this is its synchronous equivalent,
    for callers with nothing to show progress to."""
    state = start_draft(store, recipe, now, seed, previous)
    while state["pending"]:
        add_other_input(state, store, client, state["pending"].pop(0))
    fill_own_facts(state, store, now)
    return finish_draft(state, store, now)


def normalized(state: DraftState, store=None, now=0.0) -> DraftState:
    """Bring a stored draft up to the current shape. A draft is persisted JSON, so one written by an
    earlier build can outlive the code that wrote it (the page reopens the last draft); every field
    added since then has to arrive with a default rather than as a missing key the template blows up
    on. Mutates and returns `state`. With a store, repair stale artist clusters and re-pick from
    the corrected pool so reopening and saving an old draft use the same tracks."""
    normalize_shape(state)
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
        restat(state)
        state["axes"]["theirs"] = road_trip_axes.merge(state["axes"]["theirs"],
                                             [c for c in state["picked"] if c["source"] == "theirs"],
                                             state["pool"])
    for axis in state["axes"]["theirs"]:
        if axis["kind"] == "genre":
            axis["artists"] = list(_neighborhoods(state).for_genre(axis["name"]))
    for key in ("minutes", "own_count", "their_count", "own_minutes", "their_minutes"):
        state.setdefault("stats", {}).setdefault(key, 0)
    state["stats"].setdefault("short", {})
    restat(state)
    for party in ("mine", "theirs"):
        state["axes"][party] = road_trip_axes.normalize(state["axes"][party], state["targets"][party])
    return state


def set_share(state: DraftState, party, axis, share, store, now=0.0) -> DraftState:
    """Set a requested share and proportionally rebalance the other genres or years on that side.

    Your side needs no widening: it IS your whole collection, so whatever rock you own is already in
    play. Theirs is finite and bought over the network, so a request bigger than their pool queues a
    YouTube search, run in the background by the caller (state["pending"]), classified on arrival and
    filtered to the axis so a loose search can't pollute the mix."""
    if party not in ("mine", "theirs") or not axis.startswith(("genre:", "era:")):
        return state
    share = max(0.0, min(1.0, float(share)))
    road_trip_axes.rebalance(state["axes"].setdefault(party, []),
                             state["targets"].setdefault(party, {}), axis, share)
    if party == "theirs":
        budget = state["target_minutes"] * 60 * (100 - state["own_pct"]) / 100.0
        prefix = axis.split(":", 1)[0] + ":"
        for key, target in state["targets"][party].items():
            if not key.startswith(prefix) or target <= 0:
                continue
            needs_context = (key.startswith("genre:") and state["inputs"].get("artists")
                             and any(not c.get("cluster_genre") for c in state["pool"]
                                     if road_trip_axes.matches(c, key)))
            if not needs_context and road_trip_axes.available_seconds(state["pool"], key) >= target * budget:
                continue
            queued = set(state.get("done_widening", [])) | {p.get("widen_key") for p in state["pending"]}
            fresh = []
            for term in road_trip_axes.widen_terms(state["pool"], state.get("inputs", {}).get("genres", []), key):
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


def clear_share(state: DraftState, party, axis, store, now=0.0) -> DraftState:
    """Release a balanced group so its genres or years follow the available tracks again."""
    targets = (state.get("targets") or {}).get(party, {})
    prefix = axis.split(":", 1)[0] + ":"
    for key in list(targets):
        if key.startswith(prefix):
            del targets[key]
    return repick(state, store, now)
