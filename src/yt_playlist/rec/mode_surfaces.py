"""Taste modes across the Home surfaces (issue #60, Part B).

The worker buckets every surface's candidate pool by nearest taste mode (prepare_bundles); the Home
request cheaply selects 4 distinct modes by acceptance-weighted Thompson sampling and live mood
(#87), assigns one per card, and tilts/orders/caps within the prepared bucket (assemble_cards). No
pool ranking happens on the request once full-depth bundles have been prepared."""
import hashlib
import json
import random
from collections import Counter
from itertools import permutations

import numpy as np

from yt_playlist.rec import (embed, experiments, home_themes, home_vibe, layers, mode_eval, rec_params, surfaces,
                             taste_modes, transient, measurement, negative)
from yt_playlist.util import genre_map

CARD_SURFACES = ("wheelhouse", "explore", "comfort", "fresh")
_MIN_CARD = 4          # below this many tracks (even after backfill) a card is dropped, not shown thin

_PPR_TAIL = 1 << 30    # sort sentinel: a candidate absent from the PPR ranking sorts after all ranked ones


def _ranker_for(epoch, mode_id, share) -> str:
    """Deterministic A/B coin per (epoch, mode): 'ppr' with probability `share`, else 'cosine'. Seeded
    with an integer (str seeds are per-process-randomized), so the same card resolves to the same ranker
    across processes and re-renders of the epoch."""
    seed = (int(epoch) * 1_000_003 + int(mode_id)) & 0x7FFFFFFF
    return "ppr" if random.Random(seed).random() < share else "cosine"


def _order_key(ranker, ppr_pos, leans, penalties=None):
    """Sort key for a card's candidates: ascending PPR position for the 'ppr' arm (unknown keys last),
    else the existing descending mood-tilt for the 'cosine' arm. Both return ascending-sortable values."""
    penalties = penalties or {}
    if ranker == "ppr":
        # Put positions on a unit scale so the bounded dislike delta has the same strength
        # whatever depth the cached PPR ranking contains. Missing candidates still follow it.
        scale = max(1, len(ppr_pos)) if any(penalties.values()) else 1
        return lambda d: (ppr_pos.get(d.get("key"), _PPR_TAIL) / scale
                          - penalties.get(d.get("key"), 0.0))
    return lambda d: -_tilt_key(d, leans) - penalties.get(d.get("key"), 0.0)


def _item_dict(it) -> dict:
    """A ForYouItem flattened to the fields the request needs to tilt/order/cap/render without
    reloading vectors."""
    return {"key": getattr(it, "key", ""), "video_id": it.video_id, "title": it.title,
            "artist": it.artist, "album": getattr(it, "album", ""), "thumbnail": it.thumbnail,
            "plays": getattr(it, "plays", 0), "reason": getattr(it, "reason", ""),
            "lane": getattr(it, "lane", ""), "genre": getattr(it, "genre", "") or "",
            "year": getattr(it, "year", None), "score": getattr(it, "score", None)}


def _nearest_mode(key, mode_ids, C, lidx, LV, didx, DV):
    """mode_id of the nearest mode centroid to this track's content vector (library first, then
    discovered), or None when the track has no content vector."""
    if LV is not None and key in lidx:
        v = LV[lidx[key]].astype(np.float64)
    elif DV is not None and key in didx:
        v = DV[didx[key]].astype(np.float64)
    else:
        return None
    return mode_ids[int((C @ v).argmax())]


def _bucket_pool(pool, mode_ids, C, lidx, LV, didx, DV, cap):
    buckets = {str(mid): [] for mid in mode_ids}
    for it in pool:
        mid = _nearest_mode(getattr(it, "key", ""), mode_ids, C, lidx, LV, didx, DV)
        if mid is not None and len(buckets[str(mid)]) < cap:
            buckets[str(mid)].append(_item_dict(it))
    return buckets


def refresh_fresh(store, pool, now):
    """Rebucket newly ranked discoveries after enrichment without rebuilding library surfaces."""
    snapshot = store.rec_model.content_generation()
    modes = taste_modes.live_modes(store, snapshot=snapshot)
    mode_ids = [m["mode_id"] for m in modes]
    buckets = {}
    if modes:
        C = np.stack([m["centroid"].astype(np.float64) for m in modes])
        _, DV, didx = embed.load_discovered_content_vectors(store, snapshot=snapshot)
        cap = int(rec_params.get_param(store, "modes_cand_per_mode"))
        buckets = _bucket_pool(pool, mode_ids, C, {}, None, didx, DV, cap)
    # Merge into the latest payload so a concurrent library rebuild cannot lose its surfaces.
    with store._lock:
        payload = store.rec_surface.get_proposals("mode_bundles")
        if not payload:
            return
        for mid, bucket in payload.items():
            if mid != "all" and not mid.startswith("_"):
                bucket["fresh"] = buckets.get(mid, [])
        for mid, items in buckets.items():
            payload.setdefault(mid, {})["fresh"] = items
        fresh = measurement.stamp_pool(store, [_item_dict(it) for it in pool], now, "cold")
        versions = {item["key"]: item.get("pool_policy") for item in fresh}
        for bucket in payload.values():
            if isinstance(bucket, dict):
                for item in bucket.get("fresh", []):
                    item["pool_policy"] = versions.get(item.get("key"))
        payload.setdefault("all", {})["fresh"] = fresh
        store.rec_surface.put_proposals("mode_bundles", payload, now)


def prepare_bundles(store, now) -> dict:
    """Bucket each surface's pool by nearest mode; cache under rec_proposals['mode_bundles']. Payload
    {str(mode_id): {surface: [item_dict, ...]}}. Empty dict when there are no modes."""
    snapshot = store.rec_model.content_generation()
    modes = taste_modes.live_modes(store, snapshot=snapshot)
    if not modes:
        store.rec_surface.put_proposals("mode_bundles", {}, now)
        # Fresh also works before metadata coverage is sufficient to form taste modes.
        pool = surfaces.cold_candidates(store, now, limit=None)
        store.rec_surface.put_proposals("fresh_pool", measurement.stamp_pool(
            store, [surfaces._item_to_fresh_dict(t) for t in pool], now, "cold"), now)
        return {}
    mode_ids = [m["mode_id"] for m in modes]
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])
    lkeys, LV, lidx = embed.load_content_vectors(store, snapshot=snapshot)
    dkeys, DV, didx = embed.load_discovered_content_vectors(store, snapshot=snapshot)
    cap = int(rec_params.get_param(store, "modes_cand_per_mode"))
    # Rank through the library before bucketing. A global top-400 cutoff can leave a substantial
    # genre with only six candidates even when the library contains hundreds of matching songs.
    lim = max(len(lkeys), int(rec_params.get_param(store, "modes_pool_limit")))
    pools = {
        "wheelhouse": surfaces.for_you(store, now, limit=lim),
        "explore": surfaces.explore_for_you(store, now, limit=lim),
        "comfort": surfaces.comfort_listening(store, now, limit=lim),
        "fresh": surfaces.cold_candidates(store, now, limit=None),
    }
    payload = {str(mid): {} for mid in mode_ids}
    for surf, pool in pools.items():
        buckets = _bucket_pool(pool, mode_ids, C, lidx, LV, didx, DV, cap)
        for mid in mode_ids:
            payload[str(mid)][surf] = buckets[str(mid)]
        # Keep the full ranked surface for backfill: the global head can contain almost none of
        # a user's smaller genre families, even when they have plenty of eligible library tracks.
        payload.setdefault("all", {})[surf] = [_item_dict(it) for it in pool
                                               if getattr(it, "key", "")]
        stamped = measurement.stamp_pool(store, payload["all"][surf], now, surf)
        versions = {item["key"]: item.get("pool_policy") for item in stamped}
        payload["all"][surf] = stamped
        for mid in mode_ids:
            for item in payload[str(mid)][surf]:
                item["pool_policy"] = versions.get(item.get("key"))
    # Compute the library-relative bands before capping temporal candidates: a mode's top matches
    # can all be old releases even when its middle and newer bands have plenty of eligible tracks.
    years = store.modes.years_for(lkeys) if lkeys else {}
    yvals = sorted(y for y in years.values() if y)
    cuts = ([int(np.percentile(yvals, 33)), int(np.percentile(yvals, 66))]
            if len(yvals) >= 30 else None)
    # Temporal surface (#63): each mode's own library member tracks, carrying release year, for the
    # date-banded 4th card (Throwback / Time Flies / Recent Picks). Built from content-vector
    # membership, not a scorer pool, so it must apply the same hard suppression the scorers do
    # (YouTube dislikes + dismiss/mute/snooze) or a thumbs-down track leaks back into the card.
    _build_temporal(store, payload, mode_ids, C, lkeys, LV, cap, years=years, year_cuts=cuts,
                    suppressed=store.rec_model.suppressed_keys("for_you", now))
    for mode_id in mode_ids:
        bucket = payload.get(str(mode_id), {})
        if bucket.get("temporal"):
            bucket["temporal"] = measurement.stamp_pool(store, bucket["temporal"], now, "temporal")
    # Meta: Comfort's credibility signal (its pool size) + the user's release-year band cuts (terciles).
    payload["_meta"] = {"comfort_pool": len(pools["comfort"]), "year_cuts": cuts,
                        "full_pools": True}
    # #57 per-mode PPR ordering, precomputed for the A/B in-mode ranker. Best-effort: a failure leaves
    # cards on the existing ranker (assemble_cards falls back when a mode has no _ppr entry).
    try:
        from yt_playlist.rec import ppr
        rankings = ppr.mode_rankings(store, snapshot=snapshot)
        payload["_ppr"] = {str(mid): rankings.get(mid, []) for mid in mode_ids}
        payload["_meta"]["ppr_policy"] = {
            "algorithm": "basket-ppr-v1", "built_at": now,
            "params": {name: rec_params.get_param(store, name) for name in
                       ("ppr_alpha", "ppr_iters", "ppr_tol", "ppr_rank_depth")},
            "ranking_fingerprint": hashlib.sha256(json.dumps(payload["_ppr"], sort_keys=True)
                                                   .encode()).hexdigest()}
    except Exception:  # noqa: BLE001 - PPR precompute must never break bundle prep
        payload["_ppr"] = {}
    store.rec_surface.put_proposals("mode_bundles", payload, now)
    # Supersede the last on-demand pool too, so explicit-genre Fresh previews use the worker's
    # latest metadata/ranking rather than keeping an older clicked job's snapshot forever.
    store.rec_surface.put_proposals("fresh_pool", payload.get("all", {}).get("fresh", []), now)
    return payload


def _temporal_item(key, meta, genres, years) -> dict:
    m = meta.get(key, {})
    return {"key": key, "video_id": m.get("video_id"), "title": m.get("title") or "",
            "artist": m.get("artist") or "", "album": m.get("album") or "",
            "thumbnail": m.get("thumbnail"), "plays": 0, "reason": "", "lane": "temporal",
            "genre": genres.get(key) or "", "year": years.get(key)}


def _build_temporal(store, payload, mode_ids, C, lkeys, LV, cap, *, years, year_cuts,
                    suppressed=frozenset()) -> None:
    """Per mode, its library member tracks (nearest the mode centroid, top `cap` per release-year
    band), under payload[mode]['temporal'] + a general 'all' temporal pool. `suppressed` keys
    (YouTube dislikes, dismiss/mute/snooze) are dropped so this membership surface honours feedback
    the same way the scorer pools do."""
    if LV is None or not lkeys:
        for mid in mode_ids:
            payload[str(mid)]["temporal"] = []
        payload.setdefault("all", {})["temporal"] = []
        return
    Vf = LV.astype(np.float64)
    sims = Vf @ C.T                                # (n_tracks, n_modes) cosine
    near = sims.argmax(axis=1)
    meta = store.rec_query.tracks_by_keys(lkeys)
    genres = store.modes.genres_for(lkeys)
    allt = []
    for j, mid in enumerate(mode_ids):
        rows = np.where(near == j)[0]
        rows = rows[np.argsort(-sims[rows, j])]
        rows = [i for i in rows if lkeys[i] not in suppressed]
        items = [_temporal_item(lkeys[i], meta, genres, years) for i in rows]
        if year_cuts is None:
            payload[str(mid)]["temporal"] = items[:cap]
        else:
            candidates = []
            for band in range(3):
                band_items = [d for d in items if _in_band(d["year"], band, *year_cuts)]
                candidates.extend(band_items[:cap])
            payload[str(mid)]["temporal"] = candidates
        allt.extend(items)
    payload.setdefault("all", {})["temporal"] = allt


def _mode_mood_weight(mode, leans):
    """Fold the live transient leans onto a mode: sum the signed leans of its families (a 'more house'
    lean lifts house-heavy modes). Returns a positive multiplier centered on 1.0."""
    s = 0.0
    for fam, cnt in mode.get("families", []):
        s += leans.get(f"genre:{fam}", 0.0) * cnt
    total = sum(c for _f, c in mode.get("families", [])) or 1
    # Clamp both ends: a floor keeps a muted mode eligible, a ceiling stops a strongly-leaned mode from
    # dominating the weighted draw so hard that the dominant never rotates (the freshness goal).
    return min(4.0, max(0.05, 1.0 + s / total))


def thompson_mode_scores(stats, mode_ids, rng) -> dict:
    """#87 One Beta posterior sample per mode: Beta(1 + picks, 1 + impressions - picks). A mode
    that gets offered but never picked concentrates low and is served less; an unproven mode's
    wide Beta(1,1) keeps it explored. The sample replaces the old draw's uniform randomness, so
    exploration is uncertainty-shaped instead of blind."""
    out = {}
    for mid in mode_ids:
        picks, imps = stats.get(mid, (0, 0))
        out[mid] = rng.betavariate(*mode_eval.choice_posterior(picks, imps))
    return out


def select_modes(store, modes, leans, epoch, n=4, stats=None, now_posterior=None) -> list[int]:
    """Pick n distinct mode_ids using acceptance evidence across all card framings. The first uses
    Thompson-sampled pick-through x library share x live mood x NOW-layer boost (#88). Later choices
    use that same evidence with a bounded diversity bonus, so an ignored theme cannot keep returning
    just because its centroid is distant. Deterministic for fixed inputs.

    `now_posterior` ({mode_id: share} from `layers.now_mode_posterior`, or None) multiplies the
    dominant draw's context: a mode carrying share `s` of the last `now_window_h` hours' real plays
    gets `now_boost = 1.0 + now_gain * s`, nudging the rotation toward whatever the listener is doing
    right now without ever excluding a mode outright. `now_gain` (a live param) is read ONLY when a
    posterior is present, so a None posterior (the common case: quiet hours, thin evidence, or a caller
    that doesn't pass one, including `store=None` in tests) touches no param and reproduces the exact
    pre-#88 behavior, byte-for-byte: no boost, no store access."""
    if not modes:
        return []
    rng = random.Random(epoch)
    cents = {m["mode_id"]: np.asarray(m["centroid"], dtype=np.float64) for m in modes}
    now_gain = rec_params.get_param(store, "now_gain") if now_posterior else None

    def _now_boost(mid):
        if not now_posterior:
            return 1.0
        return 1.0 + now_gain * now_posterior.get(mid, 0.0)

    # #87 Thompson-sampled dominant: sample each mode's pick-through posterior, scale by the same
    # library-share and mood context as before, take the max. The sample's randomness IS the
    # rotation (a big or mood-lifted mode rolls dominant often but not always), and unlike the old
    # blind draw it is uncertainty-shaped: offered-but-never-picked modes concentrate low, unproven
    # modes stay wide and keep getting explored. Zero pick data reproduces the old behavior in
    # expectation (uniform samples scale every mode equally).
    samples = thompson_mode_scores(stats or {}, [m["mode_id"] for m in modes], rng)
    scores = {m["mode_id"]: samples[m["mode_id"]] * max(1, m["size"])
              * _mode_mood_weight(m, leans) * _now_boost(m["mode_id"]) for m in modes}
    dominant = max(scores, key=scores.get)
    chosen = [dominant]
    remaining = [m["mode_id"] for m in modes if m["mode_id"] != dominant]
    while len(chosen) < min(n, len(modes)):
        def diverse_score(mid):
            distance = min(1.0 - float(cents[mid] @ cents[c]) for c in chosen)
            return scores[mid] * (1.0 + max(0.0, min(2.0, distance)))

        nxt = max(remaining, key=diverse_score)
        chosen.append(nxt)
        remaining.remove(nxt)
    return chosen


def artist_cap(items, max_per) -> list:
    """Drop items beyond max_per per artist, preserving order."""
    seen, out = {}, []
    for d in items:
        a = d.get("artist", "")
        seen[a] = seen.get(a, 0) + 1
        if seen[a] <= max_per:
            out.append(d)
    return out


PROTO_SIZE = 12
_CARD_LABELS = {"wheelhouse": "More in your wheelhouse", "explore": "From your catalog",
                "comfort": "Comfort listening", "fresh": "Fresh songs"}


def _tilt_key(d, leans):
    """A light, vector-free transient score for ordering within a bucket: the track's genre lean
    multiplier (centered on 1.0). Higher sorts first."""
    fam = d.get("genre", "")
    return 1.0 + leans.get(f"genre:{fam}", 0.0)


def _diversify(items, max_artist, max_album) -> list:
    """Cap tracks per artist and per album, preserving order. An empty album ('' = a single) is never
    album-capped (each single is its own thing). Kills 'one compilation fills the card' / 'four artists
    over twelve tracks'."""
    a, al, out = Counter(), Counter(), []
    for d in items:
        ar, ab = d.get("artist", ""), (d.get("album") or "")
        if a[ar] >= max_artist:
            continue
        if ab and al[ab] >= max_album:
            continue
        a[ar] += 1
        if ab:
            al[ab] += 1
        out.append(d)
    return out


_BAND_LABELS = {0: "Throwback", 1: "Time Flies", 2: "Recent Picks"}   # temporal card label by band (#63)


def _in_band(year, band, lo, hi) -> bool:
    """Is a release year in the epoch's date band? 0 = older (<=lo), 1 = middle, 2 = newer (>hi). A track
    with no known year is never in any band (excluded from the temporal card)."""
    if year is None:
        return False
    if band == 0:
        return year <= lo
    if band == 1:
        return lo < year <= hi
    return year > hi


def assemble_cards(store, now, epoch, *, avoid_genres=(), include_genres=()) -> list[dict]:
    """Build the mode-focused Home cards from the prepared bundles. Three always-on surfaces
    (wheelhouse, explore, fresh) plus a resolved 4th slot: COMFORT if its pool is credible
    (>= comfort_min_pool), else the TEMPORAL card whose band rotates per epoch (Throwback / Time Flies /
    Recent Picks) over the user's own release-year terciles. Modes are selected by Thompson-sampled
    pick-through x library share x live mood (#87), NOW-boosted (#88, see `select_modes`), and
    assigned by lane coverage, usable card depth, then selection priority. Extra tracks beyond a full card do not
    pin a theme to a framing (temporal depth is measured within the epoch's band).
    Each card is diversity-capped (artist + album) and, EXCEPT
    Comfort, backfilled from its general pool when thin; Comfort shows only real comfort tracks. If a
    Comfort slot comes up thinner than _MIN_CARD after capping (its global pool can be credible while
    the assigned mode's bucket is not), TEMPORAL rotates into the 4th slot so the row stays at four,
    rather than dropping to three. Returns [] when there are no bundles/modes (caller falls back to
    legacy cards)."""
    bundles = store.rec_surface.get_proposals("mode_bundles")
    modes = store.modes.list_modes(active_only=True)
    if not bundles or not modes:
        return []
    original_bundles = bundles
    if avoid_genres or include_genres:
        # Start each card in the requested families. Backfill searches the complete pool below,
        # widening to close neighbors only after exhausting its own family. Keep caches untouched.
        bundles = {mid: ({surf: home_themes.matching_genres(store, items, avoid=avoid_genres,
                                                          include=include_genres)
                          for surf, items in bucket.items()}
                        if mid != "all" and not mid.startswith("_") else bucket)
                   for mid, bucket in bundles.items()}
        # Keep the feedback attribution honest: stripping jazz from a jazz-led mode must not
        # turn its few remaining rock tracks into a card whose pick still trains the jazz mode.
        modes = [m for m in modes
                 if (not m.get("families") or max(m["families"], key=lambda f: f[1])[0] not in avoid_genres)
                 and (not include_genres or not m.get("families")
                      or max(m["families"], key=lambda f: f[1])[0] in include_genres)
                 and any(len(items) >= _MIN_CARD
                         for items in bundles.get(str(m["mode_id"]), {}).values())]
        if not modes:
            return []
    leans = transient.facet_leans(store, now)
    vibe_rank = home_vibe.ranker(store, now)
    n = int(rec_params.get_param(store, "modes_menu_size"))
    now_posterior = layers.now_mode_posterior(store, now)
    evidence_before = store.modes.epoch_started_at(epoch)
    chosen = select_modes(store, modes, leans, epoch, n=len(modes) if vibe_rank.preset else max(n, 4),
                          stats=mode_eval.mode_bandit_stats(store, before=evidence_before),
                          now_posterior=now_posterior)
    if not chosen:
        return []
    cap_a = int(rec_params.get_param(store, "modes_artist_cap"))
    cap_al = int(rec_params.get_param(store, "modes_album_cap"))
    meta = bundles.get("_meta", {})
    ppr_map = bundles.get("_ppr", {})
    ab_share = float(rec_params.get_param(store, "ppr_ab_share"))

    # Resolve the 4th slot. Comfort needs a credible pool; otherwise the temporal card takes the slot
    # (when the library has enough year data), its band chosen by the epoch.
    comfort_ok = meta.get("comfort_pool", 0) >= int(rec_params.get_param(store, "comfort_min_pool"))
    year_cuts = meta.get("year_cuts")
    band = epoch % 3
    fourth = "comfort" if comfort_ok else ("temporal" if year_cuts is not None else None)
    active = ["wheelhouse", "explore", "fresh"] + ([fourth] if fourth else [])

    def _bucket(mid, surf):
        b = list((bundles.get(str(mid)) or {}).get(surf, []))
        if surf == "temporal" and year_cuts is not None:
            b = [d for d in b if _in_band(d.get("year"), band, year_cuts[0], year_cuts[1])]
        return b

    generated = set(store.rec_query.generated_track_keys())
    blocked = generated | store.rec_model.suppressed_keys("for_you", now)
    muted = store.rec_model.muted_artists()

    if vibe_rank.preset:
        # An explicit vibe can also change the styles offered, rather than just reshuffle
        # twelve intense songs inside an intense mode. Preserve learned order on ties.
        def sound_match(mid):
            values = sorted((vibe_rank.affinity(d) for surf in active for d in _bucket(mid, surf)
                             if d.get("key") and d["key"] not in blocked and d.get("artist") not in muted),
                            reverse=True)
            head = values[:PROTO_SIZE]
            return sum(head) / len(head) if head else 0.5

        chosen.sort(key=sound_match, reverse=True)
        chosen = chosen[:max(n, 4)]

    # Depth is a fillability constraint, not a theme preference. Once a bucket can fill a card,
    # respect the sampled theme priority instead of always handing the deepest mode the same slot.
    # Count usable tracks: saved songs and diversity-dropped rows cannot fill the preview.
    def _depth(mid, surf):
        items = [d for d in _bucket(mid, surf) if d.get("key") and d["key"] not in blocked
                 and d.get("artist") not in muted]
        return min(PROTO_SIZE, len(_diversify(items, cap_a, cap_al)))

    depths = {(surf, mid): _depth(mid, surf) for surf in active for mid in chosen}
    priority = {mid: i for i, mid in enumerate(chosen)}

    def assignment_score(mids):
        sizes = [depths.get((surf, mid), 0) for surf, mid in zip(active, mids)]
        return (sum(size >= _MIN_CARD for size in sizes), sum(sizes),
                tuple(-priority.get(mid, len(chosen)) for mid in mids))

    # Consider the whole row before spending a mode that may be a lane's only option.
    # At most six modes compete for four slots (360 assignments). Prefer credible
    # cards, then capped depth, then sampled priority in display order. Empty slots
    # let a smaller menu put its modes where tracks are available.
    candidates = chosen + [None] * max(0, len(active) - len(chosen))
    best = max(permutations(candidates, len(active)), key=assignment_score)
    assign = {surf: mid for surf, mid in zip(active, best) if mid is not None}

    # Songs already bundled into a generated playlist are spoken for, and must not be offered again
    # however stale the bundles are. The bundles apply this exclusion when they are BUILT, but
    # creating a playlist does not rebuild them, so every card between one build and the next handed
    # back exactly what you had just bundled: generate twice in a day and you got the same tracks.
    # Seeding `seen` makes it a render-time fact rather than a build-time snapshot, and reuses the
    # per-render machinery that already blocks a track across cards.
    cards, seen = [], set(blocked)
    full_pools = {}
    dislike_deltas = {}

    def ordering(ranker, positions, items):
        missing = {d["key"] for d in items if d.get("key") and d["key"] not in dislike_deltas}
        if missing:
            deltas = negative.for_keys(store, missing)
            dislike_deltas.update({key: deltas.get(key, 0.0) for key in missing})
        return _order_key(ranker, positions, leans, dislike_deltas)

    mode_families = {m["mode_id"]: max(m["families"], key=lambda f: f[1])[0]
                     for m in modes if m.get("families")}

    def _full_pool(surf):
        if surf not in full_pools:
            pool = list(original_bundles.get("all", {}).get(surf, []))
            # Older caches only kept 40 global candidates. Include their other mode buckets, then
            # reach through the ranked library now; the next worker rebuild caches that full depth.
            if not meta.get("full_pools"):
                for mid, bucket in original_bundles.items():
                    if mid != "all" and not mid.startswith("_"):
                        pool.extend(bucket.get(surf, []))
                if surf in ("wheelhouse", "explore"):
                    keys, _, _ = embed.load_content_vectors(store)
                    limit = max(len(keys), int(rec_params.get_param(store, "modes_pool_limit")))
                    scorer = surfaces.for_you if surf == "wheelhouse" else surfaces.explore_for_you
                    pool.extend(_item_dict(it) for it in scorer(store, now, limit=limit))
            unique = {}
            for item in pool:
                if item.get("key"):
                    unique.setdefault(item["key"], item)
            pool = home_themes.resolved_genres(store, list(unique.values()))
            full_pools[surf] = [d for d in pool if d.get("artist") not in muted
                                and genre_map.family(d.get("genre")) not in avoid_genres]
        return full_pools[surf]

    def _card_for(surf, mid, ranker, ppr_pos):
        """Build one card for (surface, mode), or return (None, bucket) when it is too thin. Reads the
        running `seen` set from the enclosing scope."""
        bucket = _bucket(mid, surf)
        items = [d for d in bucket if d.get("key") and d["key"] not in seen
                 and d.get("artist") not in muted]
        candidate_keys = {d["key"] for d in items}
        items.sort(key=ordering(ranker, ppr_pos, items))
        items = vibe_rank(items)
        items = _diversify(items, cap_a, cap_al)
        if surf != "comfort" and len(items) < PROTO_SIZE:   # Comfort is NEVER backfilled (#63 credibility)
            taken = {d["key"] for d in items}
            pool = _full_pool(surf)
            if surf == "temporal" and year_cuts is not None:
                pool = [d for d in pool if _in_band(d.get("year"), band, year_cuts[0], year_cuts[1])]
            extra = [d for d in pool if d.get("key") and d["key"] not in seen and d["key"] not in taken]
            family = mode_families.get(mid)
            order = ordering(ranker, ppr_pos, extra)
            if family:
                distance = lambda d: genre_map.family_distance(family, genre_map.family(d.get("genre")))
                # Exhaust the SAME family across the whole library before adding close neighbors.
                # Never fill a soul mix with unrelated electronic tracks just because both were selected.
                extra = [d for d in extra if d.get("genre") and distance(d) <= 0.5]
                extra.sort(key=lambda d: (distance(d), order(d)))
            else:
                extra = home_themes.matching_genres(store, extra, include=include_genres)
                extra.sort(key=order)
            candidate_keys.update(d["key"] for d in extra)
            extra = vibe_rank(extra)
            items = _diversify(items + extra, cap_a, cap_al)
        items = items[:PROTO_SIZE]
        if len(items) < _MIN_CARD:                          # credibility gate: too thin to show
            return None, bucket
        label = _BAND_LABELS[band] if surf == "temporal" else _CARD_LABELS[surf]
        # The candidate-fit score is not the final assembly order: preserve both its origin and
        # the ordering inputs, before diversity, genre backfill and the later DJ journey.
        items = [{**item, "serving_context": {"score_kind": "candidate_fit", "source_ranker": surf,
                  "ordering_ranker": ranker, "ppr_position": ppr_pos.get(item["key"]) if ranker == "ppr" else None,
                  "facet_tilt": _tilt_key(item, leans), "vibe": vibe_rank.preset,
                  "negative_delta": dislike_deltas.get(item["key"], 0.0),
                  "vibe_affinity": vibe_rank.affinity(item)}} for item in items]
        return {"lane": surf, "label": label, "mode_id": mid, "ranker": ranker,
                "tracks": items, "_candidate_keys": candidate_keys}, bucket

    def _commit(card, bucket):
        # Replay the other ordering against this opportunity before the live card changes `seen`.
        # Nothing is persisted by previews; only the route's final rendered card becomes exposure.
        mid, surf = card["mode_id"], card["lane"]
        positions = {k: i for i, k in enumerate(ppr_map.get(str(mid), []))}
        decision = experiments.last_decision(store)
        if positions and not (ab_share == 0 and decision.get("decision") == "retire"):
            other = "cosine" if card["ranker"] == "ppr" else "ppr"
            replay, _ = _card_for(surf, mid, other, positions)
            if replay:
                candidates = sorted(card["_candidate_keys"] | replay["_candidate_keys"])
                policy = meta.get("ppr_policy", {"algorithm": "unknown-legacy-cache"})
                cohort_policy = {k: v for k, v in policy.items() if k != "built_at"}
                cohort = hashlib.sha256(json.dumps({"policy": cohort_policy, "share": ab_share,
                    "caps": [cap_a, cap_al], "vibe": vibe_rank.preset,
                    "leans": leans, "negative": {k: dislike_deltas[k] for k in candidates
                                                 if dislike_deltas.get(k)}},
                    sort_keys=True).encode()).hexdigest()[:16]
                card["experiment"] = {
                    "schema": 1, "name": "ppr-cosine-v1", "cohort": cohort,
                    "cluster": f"{epoch}:{mid}", "share": ab_share,
                    "state": "shadow" if ab_share == 0 else "retained" if ab_share == 1 else "live",
                    "candidate_fingerprint": hashlib.sha256(json.dumps(candidates).encode()).hexdigest(),
                    "candidate_count": len(candidates), "ppr_policy": policy}
                card["counterfactual"] = {"ranker": other, "tracks": replay["tracks"]}
        # Block the ENTIRE considered bucket (rendered + diversity-dropped), not just the rendered rows,
        # so a track this card dropped can't reappear via a later card's backfill pool.
        seen.update(d["key"] for d in bucket if d.get("key"))
        seen.update(d["key"] for d in card["tracks"])
        cards.append(card)

    for surf in active:
        mid = assign.get(surf)
        if mid is None:
            continue
        ranker = _ranker_for(epoch, mid, ab_share)
        ppr_pos = {k: i for i, k in enumerate(ppr_map.get(str(mid), []))}
        if ranker == "ppr" and not ppr_pos:   # no PPR data for this mode: order and label honestly
            ranker = "cosine"
        card, bucket = _card_for(surf, mid, ranker, ppr_pos)
        if card is not None:
            _commit(card, bucket)
            continue
        # 4th-slot rotation: comfort_ok was checked against the GLOBAL pool, but the actual card is one
        # mode's bucket, which can fall under _MIN_CARD after diversity capping. Rather than drop the
        # slot (leaving three cards), rotate TEMPORAL in when the library has year data. So the order is
        # comfort -> temporal -> (drop only if neither fills).
        if surf == "comfort" and year_cuts is not None:
            used = {c["mode_id"] for c in cards}
            avail = sorted((m for m in chosen if m not in used),
                           key=lambda m: -len(_bucket(m, "temporal")))   # deepest band bucket first
            for tmid in avail:
                tranker = _ranker_for(epoch, tmid, ab_share)
                tpos = {k: i for i, k in enumerate(ppr_map.get(str(tmid), []))}
                if tranker == "ppr" and not tpos:   # no PPR data for this mode: order and label honestly
                    tranker = "cosine"
                tcard, tbucket = _card_for("temporal", tmid, tranker, tpos)
                if tcard is not None:
                    _commit(tcard, tbucket)
                    break
    return cards
