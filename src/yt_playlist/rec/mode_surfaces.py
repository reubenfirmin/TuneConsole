"""Taste modes across the Home surfaces (issue #60, Part B).

The worker buckets every surface's candidate pool by nearest taste mode (prepare_bundles); the Home
request cheaply selects 4 distinct modes by acceptance-weighted Thompson sampling and live mood
(#87), assigns one per card, and tilts/orders/caps within the prepared bucket (assemble_cards). No
pool ranking happens on the request."""
import random
from collections import Counter

import numpy as np

from yt_playlist.rec import embed, layers, mode_eval, rec_params, recommend, surfaces, transient

CARD_SURFACES = ("wheelhouse", "explore", "comfort", "fresh")
_MIN_CARD = 4          # below this many tracks (even after backfill) a card is dropped, not shown thin

_PPR_TAIL = 1 << 30    # sort sentinel: a candidate absent from the PPR ranking sorts after all ranked ones


def _ranker_for(epoch, mode_id, share) -> str:
    """Deterministic A/B coin per (epoch, mode): 'ppr' with probability `share`, else 'cosine'. Seeded
    with an integer (str seeds are per-process-randomized), so the same card resolves to the same ranker
    across processes and re-renders of the epoch."""
    seed = (int(epoch) * 1_000_003 + int(mode_id)) & 0x7FFFFFFF
    return "ppr" if random.Random(seed).random() < share else "cosine"


def _order_key(ranker, ppr_pos, leans):
    """Sort key for a card's candidates: ascending PPR position for the 'ppr' arm (unknown keys last),
    else the existing descending mood-tilt for the 'cosine' arm. Both return ascending-sortable values."""
    if ranker == "ppr":
        return lambda d: ppr_pos.get(d.get("key"), _PPR_TAIL)
    return lambda d: -_tilt_key(d, leans)


def _item_dict(it) -> dict:
    """A ForYouItem flattened to the fields the request needs to tilt/order/cap/render without
    reloading vectors."""
    return {"key": getattr(it, "key", ""), "video_id": it.video_id, "title": it.title,
            "artist": it.artist, "album": getattr(it, "album", ""), "thumbnail": it.thumbnail,
            "plays": getattr(it, "plays", 0), "reason": getattr(it, "reason", ""),
            "lane": getattr(it, "lane", ""), "genre": getattr(it, "genre", "") or ""}


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


def prepare_bundles(store, now) -> dict:
    """Bucket each surface's pool by nearest mode; cache under rec_proposals['mode_bundles']. Payload
    {str(mode_id): {surface: [item_dict, ...]}}. Empty dict when there are no modes."""
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        store.put_proposals("mode_bundles", {}, now)
        return {}
    mode_ids = [m["mode_id"] for m in modes]
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])
    lkeys, LV, lidx = embed.load_content_vectors(store)
    dkeys, DV, didx = embed.load_discovered_content_vectors(store)
    cap = int(rec_params.get_param(store, "modes_cand_per_mode"))
    lim = int(rec_params.get_param(store, "modes_pool_limit"))
    pools = {
        "wheelhouse": recommend.for_you(store, now, limit=lim),
        "explore": recommend.explore_for_you(store, now, limit=lim),
        "comfort": recommend.comfort_listening(store, now, limit=lim),
        "fresh": surfaces.cold_candidates(store, now, limit=None),
    }
    payload = {str(mid): {} for mid in mode_ids}
    for surf, pool in pools.items():
        buckets = {str(mid): [] for mid in mode_ids}
        for it in pool:
            mid = _nearest_mode(getattr(it, "key", ""), mode_ids, C, lidx, LV, didx, DV)
            if mid is None:
                continue
            b = buckets[str(mid)]
            if len(b) < cap:                       # pool is pre-ranked, so first `cap` = top `cap`
                b.append(_item_dict(it))
        for mid in mode_ids:
            payload[str(mid)][surf] = buckets[str(mid)]
        # General backfill pool: the top of the whole surface (no mode filter), used to top up a card
        # whose assigned-mode bucket is thin so it reaches a full, diverse PROTO_SIZE.
        payload.setdefault("all", {})[surf] = [_item_dict(it) for it in pool[:cap]
                                               if getattr(it, "key", "")]
    # Temporal surface (#63): each mode's own library member tracks, carrying release year, for the
    # date-banded 4th card (Throwback / Time Flies / Recent Picks). Built from content-vector
    # membership, not a scorer pool, so it must apply the same hard suppression the scorers do
    # (YouTube dislikes + dismiss/mute/snooze) or a thumbs-down track leaks back into the card.
    _build_temporal(store, payload, mode_ids, C, lkeys, LV, cap,
                    store.suppressed_keys("for_you", now))
    # Meta: Comfort's credibility signal (its pool size) + the user's release-year band cuts (terciles).
    years = store.modes.years_for(lkeys) if lkeys else {}
    yvals = sorted(y for y in years.values() if y)
    cuts = ([int(np.percentile(yvals, 33)), int(np.percentile(yvals, 66))]
            if len(yvals) >= 30 else None)
    payload["_meta"] = {"comfort_pool": len(pools["comfort"]), "year_cuts": cuts}
    # #57 per-mode PPR ordering, precomputed for the A/B in-mode ranker. Best-effort: a failure leaves
    # cards on the existing ranker (assemble_cards falls back when a mode has no _ppr entry).
    try:
        from yt_playlist.rec import ppr
        rankings = ppr.mode_rankings(store)
        payload["_ppr"] = {str(mid): rankings.get(mid, []) for mid in mode_ids}
    except Exception:  # noqa: BLE001 - PPR precompute must never break bundle prep
        payload["_ppr"] = {}
    store.put_proposals("mode_bundles", payload, now)
    return payload


def _temporal_item(key, meta, genres, years) -> dict:
    m = meta.get(key, {})
    return {"key": key, "video_id": m.get("video_id"), "title": m.get("title") or "",
            "artist": m.get("artist") or "", "album": m.get("album") or "",
            "thumbnail": m.get("thumbnail"), "plays": 0, "reason": "", "lane": "temporal",
            "genre": genres.get(key) or "", "year": years.get(key)}


def _build_temporal(store, payload, mode_ids, C, lkeys, LV, cap, suppressed=frozenset()) -> None:
    """Per mode, its library member tracks (nearest the mode centroid, top `cap`), each carrying its
    release year, under payload[mode]['temporal'] + a general 'all' temporal pool. `suppressed` keys
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
    meta = store.tracks_by_keys(lkeys)
    genres = store.modes.genres_for(lkeys)
    years = store.modes.years_for(lkeys)
    allt = []
    for j, mid in enumerate(mode_ids):
        rows = np.where(near == j)[0]
        rows = rows[np.argsort(-sims[rows, j])]
        rows = [i for i in rows if lkeys[i] not in suppressed][:cap]   # most central, minus suppressed
        items = [_temporal_item(lkeys[i], meta, genres, years) for i in rows]
        payload[str(mid)]["temporal"] = items
        allt.extend(items)
    payload.setdefault("all", {})["temporal"] = allt[:cap * 2]


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
        out[mid] = rng.betavariate(1 + picks, 1 + max(0, imps - picks))
    return out


def select_modes(store, modes, leans, epoch, n=4, stats=None, now_posterior=None) -> list[int]:
    """Pick n distinct mode_ids: a DOMINANT chosen by Thompson-sampled pick-through x library share x
    live mood x NOW-layer boost (#88), then (n-1) pushed apart by centroid distance. Deterministic for
    fixed (modes, leans, epoch, stats, now_posterior).

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
    dominant = max(modes, key=lambda m: samples[m["mode_id"]] * max(1, m["size"])
                   * _mode_mood_weight(m, leans) * _now_boost(m["mode_id"]))["mode_id"]
    chosen = [dominant]
    remaining = [m["mode_id"] for m in modes if m["mode_id"] != dominant]
    while len(chosen) < min(n, len(modes)):
        nxt = max(remaining, key=lambda mid: min(1.0 - float(cents[mid] @ cents[c]) for c in chosen))
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


def assemble_cards(store, now, epoch) -> list[dict]:
    """Build the mode-focused Home cards from the prepared bundles. Three always-on surfaces
    (wheelhouse, explore, fresh) plus a resolved 4th slot: COMFORT if its pool is credible
    (>= comfort_min_pool), else the TEMPORAL card whose band rotates per epoch (Throwback / Time Flies /
    Recent Picks) over the user's own release-year terciles. Modes are selected by Thompson-sampled
    pick-through x library share x live mood (#87), NOW-boosted (#88, see `select_modes`), and
    DEPTH-AWARE assigned (each surface claims the chosen mode it has the most material for, temporal
    depth measured within the epoch's band). Each card is diversity-capped (artist + album) and, EXCEPT
    Comfort, backfilled from its general pool when thin; Comfort shows only real comfort tracks. If a
    Comfort slot comes up thinner than _MIN_CARD after capping (its global pool can be credible while
    the assigned mode's bucket is not), TEMPORAL rotates into the 4th slot so the row stays at four,
    rather than dropping to three. Returns [] when there are no bundles/modes (caller falls back to
    legacy cards)."""
    bundles = store.get_proposals("mode_bundles")
    modes = store.modes.list_modes(active_only=True)
    if not bundles or not modes:
        return []
    leans = transient.facet_leans(store, now)
    n = int(rec_params.get_param(store, "modes_menu_size"))
    now_posterior = layers.now_mode_posterior(store, now)
    chosen = select_modes(store, modes, leans, epoch, n=max(n, 4),
                          stats=mode_eval.mode_bandit_stats(store), now_posterior=now_posterior)
    if not chosen:
        return []
    cap_a = int(rec_params.get_param(store, "modes_artist_cap"))
    cap_al = int(rec_params.get_param(store, "modes_album_cap"))
    allb = bundles.get("all", {})
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

    # Depth-aware assignment (global greedy) over the ACTIVE surfaces: highest (surface, mode) depth
    # pairs first, each surface/mode used once. Temporal depth is measured within the band.
    pairs = sorted((-len(_bucket(m, surf)), active.index(surf), m, surf)
                   for surf in active for m in chosen)
    assign, used_s, used_m = {}, set(), set()
    for _negdepth, _si, m, surf in pairs:
        if surf in used_s or m in used_m:
            continue
        assign[surf] = m
        used_s.add(surf)
        used_m.add(m)

    cards, seen = [], set()

    def _card_for(surf, mid, ranker, ppr_pos):
        """Build one card for (surface, mode), or return (None, bucket) when it is too thin. Reads the
        running `seen` set from the enclosing scope."""
        bucket = _bucket(mid, surf)
        items = [d for d in bucket if d.get("key") and d["key"] not in seen]
        items.sort(key=_order_key(ranker, ppr_pos, leans))
        items = _diversify(items, cap_a, cap_al)
        if surf != "comfort" and len(items) < PROTO_SIZE:   # Comfort is NEVER backfilled (#63 credibility)
            taken = {d["key"] for d in items}
            pool = allb.get(surf, [])
            if surf == "temporal" and year_cuts is not None:
                pool = [d for d in pool if _in_band(d.get("year"), band, year_cuts[0], year_cuts[1])]
            extra = [d for d in pool if d.get("key") and d["key"] not in seen and d["key"] not in taken]
            extra.sort(key=_order_key(ranker, ppr_pos, leans))
            items = _diversify(items + extra, cap_a, cap_al)
        items = items[:PROTO_SIZE]
        if len(items) < _MIN_CARD:                          # credibility gate: too thin to show
            return None, bucket
        label = _BAND_LABELS[band] if surf == "temporal" else _CARD_LABELS[surf]
        return {"lane": surf, "label": label, "mode_id": mid, "ranker": ranker,
                "tracks": items}, bucket

    def _commit(card, bucket):
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
