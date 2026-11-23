"""Worker publication of shared ranked pools and their musical-group selections.

The detached fixed-mode math also supports historical comparisons."""
import hashlib
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass

import numpy as np

from yt_playlist.core.activity import WorkClosing, check_stopping
from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.core.policy_config import PolicyCapacityFull
from yt_playlist.rec import (embedding_vectors as vectors, experiments, group_viz, home_themes, home_vibe, mode_eval,
                             ppr, rec_params, scoring, surfaces, vibe_pools)
from yt_playlist.util import genre_map
from yt_playlist.rec.mode_selection import (select_modes, thompson_mode_scores,
    _mode_mood_weight, ranker_for as _ranker_for, PROTO_SIZE,
    CARD_LABELS as _CARD_LABELS, BAND_LABELS as _BAND_LABELS)

CARD_SURFACES = ("wheelhouse", "explore", "comfort", "fresh")
_MIN_CARD = 4          # below this many tracks (even after backfill) a card is dropped, not shown thin

# Small serving projections still guard their live settings while tolerating
# ongoing import, enrichment and playback. Full pool rounds publish captured
# inputs and do not use this dependency filter.
RANKING_CHURN_TABLES = frozenset({
    "recordings", "youtube_items", "track_sources", "library_memberships",
    "artists", "artist_aliases", "metadata_observations",
    "entry_artists", "track_artists", "playlists", "playlist_items", "playlist_provenance",
    "play_events", "player_events", "listening_observations",
    "play_event_calendar", "play_event_sources",
})

_PPR_TAIL = 1 << 30    # sort sentinel: a candidate absent from the PPR ranking sorts after all ranked ones


from yt_playlist.rec.mode_rules import bundle_metadata, _item_dict, _temporal_item, _diversify, _in_band


def _order_key(ranker, ppr_pos, leans):
    """Sort key for a card's candidates: ascending PPR position for the 'ppr' arm (unknown keys last),
    else the existing descending mood-tilt for the 'cosine' arm. Both return ascending-sortable values."""
    if ranker == "ppr":
        return lambda d: ppr_pos.get(d.get("key"), _PPR_TAIL)
    return lambda d: -_tilt_key(d, leans)


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
        check_stopping()
        mid = _nearest_mode(getattr(it, "key", ""), mode_ids, C, lidx, LV, didx, DV)
        if mid is not None and len(buckets[str(mid)]) < cap:
            buckets[str(mid)].append(_item_dict(it))
    return buckets


def _publish_typed_bundles(store, snapshot, payload, fresh, policies, settings, now):
    """Stage immutable ranks, then publish the captured generation and its settings.

    The caller still owns the preparation snapshot. Its reader is released
    before staging. Source changes never discard the completed calculation.
    """
    version = policies.generation_policy()
    # Comparison with derived output must not become a ranking dependency:
    # staging this very plan writes those tables. Exact immutable rows are read
    # separately, then their complete policy closure is owned by the claim.
    with store.read_view() as previous:
        prepared = snapshot.store.recommendations.prepare_bundles(payload, fresh,
            external_key='bundle:' + uuid.uuid4().hex, policy_version=version,
            input_revision=snapshot.store.prepared_content.serial(), created_at=now,
            pending_policies=policies.rows.values(), previous=previous.recommendations)
        selected = {version, *(slot.pool.policy_version for slot in prepared.slots),
                    *(slot.revalidated_policy for slot in prepared.slots if slot.revalidated)}
        rows = [row for key, row in policies.rows.items() if key in selected]
        bases = {}
        for row in rows:
            base = json.loads(row[1]).get(BASE_FIELD)
            if base is not None:
                bases[base] = policies.bases[base]
        old_rows, old_bases = previous.rec_ledger.captured_policies(selected - policies.rows.keys())
        rows.extend(old_rows)
        bases.update((row[0], row) for row in old_bases)
    snapshot.release_reader()
    return _DeferredBundles(prepared, snapshot, rows, list(bases.values()), settings, now, payload).publish(store)


@dataclass
class _DeferredBundles:
    prepared: object
    snapshot: object
    rows: list
    bases: list
    settings: dict
    now: float
    payload: dict

    def publish(self, store):
        try:
            store.rec_ledger.record_prepared_policies(self.rows, self.bases)
            build = store.recommendations.claim(self.prepared)
        except PolicyCapacityFull:
            # Keep only one unclaimed plan. SQL retirement progress is durable;
            # restarting simply recalculates once from the then-current inputs.
            with store._lock:
                store._deferred_bundles = self
            raise
        if not store.recommendations.stage(self.prepared, build):
            return None
        selected = ({slot.surface for slot in self.prepared.slots}
                    | {output.surface for output in self.prepared.outputs})
        absent = sorted({'home', 'fresh', 'radio', *('vibe:' + preset for preset in home_vibe.PRESETS)} - selected)

        def publish():
            store.recommendations.publish(build, self.now, retire_surfaces=absent)
            store.settings.publish_values(self.settings)

        return (build.generation_id
                if self.snapshot.publish_ordered(publish, validate_inputs=False) else None)


def _calculate_bundles(store, now, policies):
    """Publish ranked pools with exact group-version slots and a shared startup pool."""
    from yt_playlist.library import mix_listening

    snapshot = store.rec_model.content_generation()
    head = group_viz.prepared(store)
    library = store.rec_query.library_keys()
    listening = mix_listening.read(store, now)
    policies.mix_listening = listening
    pools = _rank_pools(store, now, max(len(library), int(rec_params.get_param(store, 'modes_pool_limit'))),
                        listening=listening)
    payload = unmodeled_bundles(pools)
    if not payload and not library:
        return {}, []
    if not payload:
        payload = {'all': {lane: [] for lane in pools}, '_meta': {
            'full_pools': True, 'unmodeled': True, 'comfort_pool': 0}}
    for lane, items in payload['all'].items():
        payload['all'][lane] = policies.stamp(items, lane)
    _shared_temporal(store, now, library, payload, policies)
    if head:
        _group_bundles(store, now, snapshot, head, payload)
    if not any(payload['all'].values()) and '_radio' not in payload:
        return {}, []
    vibe_pools.prepare(store, payload, now, policies)
    return payload, payload['all']['fresh']


def _shared_temporal(store, now, library, payload, policies):
    years = store.modes.years_for(library)
    dated = sorted(year for year in years.values() if year)
    cuts = [int(value) for value in np.percentile(dated, [33, 66])] if len(dated) >= 30 else None
    payload['_meta']['year_cuts'] = cuts
    library_rows = store.rec_query.tracks_by_keys(library)
    genres = store.modes.genres_for(library)
    blocked = store.rec_model.suppressed_keys('for_you', now)
    ranked = dict.fromkeys(item['key'] for item in payload['all']['wheelhouse'])
    ranked.update(dict.fromkeys(sorted(library)))
    temporal = []
    for key in ranked:
        check_stopping()
        if key in library_rows and key not in blocked:
            temporal.append(_temporal_item(key, library_rows, genres, years))
    payload['all']['temporal'] = policies.stamp(temporal, 'temporal')


def _group_bundles(store, now, snapshot, head, payload):
    members = {group['version']: [] for group in head['groups'] if group.get('leaf')}
    partition = store.taste_groups.leaf_members(head['generation'])
    for key, version in partition.items():
        check_stopping()
        if version in members:
            members[version].append(key)
    if not any(members.values()):
        return
    payload['_radio'] = {'generation': snapshot['generation'],
                         'space': vectors.content_space_id(store, snapshot=snapshot), 'members': members}
    payload['_meta'].update(unmodeled=False, group_generation=head['generation'])
    cap = int(rec_params.get_param(store, 'modes_cand_per_mode'))
    cuts = payload['_meta']['year_cuts']
    for version in members:
        payload[version] = {lane: [] for lane in payload['all']}
    for lane, items in payload['all'].items():
        band_counts = Counter()
        for item in items:
            check_stopping()
            version = partition.get(item['key'])
            if version not in members:
                continue
            bucket = payload[version][lane]
            if lane == 'temporal' and cuts:
                band = next((index for index in range(3) if _in_band(item['year'], index, *cuts)), None)
                if band is None or band_counts[version, band] >= cap:
                    continue
                band_counts[version, band] += 1
                bucket.append(item)
            elif len(bucket) < cap:
                bucket.append(item)
    # PPR is optional; acquisition failure retains cosine serving and cancellation propagates.
    params = {name: rec_params.get_param(store, name) for name in
              ('ppr_alpha', 'ppr_iters', 'ppr_tol', 'ppr_rank_depth')}
    try:
        with work_span('rec: pool PPR', level=logging.DEBUG):
            rankings = ppr.rankings_from_members(members, ppr.build_transition(store),
                alpha=params['ppr_alpha'], iters=int(params['ppr_iters']), tol=params['ppr_tol'],
                depth=int(params['ppr_rank_depth']))
        payload['_ppr'], payload['_meta']['ppr_policy'] = ppr_report(members, rankings, params, now)
    except WorkClosing:
        raise
    except Exception:
        logging.getLogger(__name__).debug('Group PageRank unavailable', exc_info=True)
        payload['_ppr'] = {}


def unmodeled_bundles(pools):
    all_pools = {name: [_item_dict(it) for it in pool if it.video_id] for name, pool in pools.items()}
    return ({'all': all_pools, '_meta': {'full_pools': True, 'unmodeled': True,
             'comfort_pool': len(pools['comfort'])}} if any(all_pools.values()) else {})


def modeled_bundles(modes, library, discovered, pools, *, generation, space, cap,
                    years, metadata, genres, suppressed):
    """Allocate detached ranked pools, radio membership and release-year bands."""
    lkeys, LV, lidx = library
    dkeys, DV, didx = discovered
    mode_ids = [m['mode_id'] for m in modes]
    C = np.stack([m['centroid'].astype(np.float64) for m in modes])
    # Radio replenishes from complete style membership, independently of the
    # finite, ranked Home card pools. Persist only keys; playback hydrates one
    # bounded lookahead and never scans the library's vector matrix.
    radio_members = {str(mid): [] for mid in mode_ids}
    for key in dict.fromkeys([*lkeys, *dkeys]):
        check_stopping()
        mid = _nearest_mode(key, mode_ids, C, lidx, LV, didx, DV)
        if mid is not None:
            radio_members[str(mid)].append(key)
    # Rank through the library before bucketing. A global top-400 cutoff can leave a substantial
    # genre with only six candidates even when the library contains hundreds of matching songs.
    payload = {str(mid): {} for mid in mode_ids}
    payload['_radio'] = {'generation': generation,
                         'space': space,
                         'members': radio_members}
    for surf, pool in pools.items():
        buckets = _bucket_pool(pool, mode_ids, C, lidx, LV, didx, DV, cap)
        for mid in mode_ids:
            payload[str(mid)][surf] = buckets[str(mid)]
        # Keep the full ranked surface for backfill: the global head can contain almost none of
        # a user's smaller genre families, even when they have plenty of eligible library tracks.
        payload.setdefault("all", {})[surf] = [_item_dict(it) for it in pool
                                               if getattr(it, "key", "")]
    # Compute the library-relative bands before capping temporal candidates: a mode's top matches
    # can all be old releases even when its middle and newer bands have plenty of eligible tracks.
    yvals = sorted(y for y in years.values() if y)
    cuts = ([int(np.percentile(yvals, 33)), int(np.percentile(yvals, 66))]
            if len(yvals) >= 30 else None)
    # Temporal surface (#63): each mode's own library member tracks, carrying release year, for the
    # date-banded 4th card (Throwback / Time Flies / Recent Picks). Built from content-vector
    # membership, not a scorer pool, so it must apply the same hard suppression the scorers do
    # (YouTube dislikes + dismiss/mute/snooze) or a thumbs-down track leaks back into the card.
    _build_temporal(payload, mode_ids, C, lkeys, LV, cap, years=years, year_cuts=cuts,
                    suppressed=suppressed, metadata=metadata, genres=genres)
    # Meta: Comfort's credibility signal (its pool size) + the user's release-year band cuts (terciles).
    payload["_meta"] = {"comfort_pool": len(pools["comfort"]), "year_cuts": cuts,
                        "full_pools": True}
    return payload


def ppr_report(mode_ids, rankings, params, now):
    values = {str(mid): rankings.get(mid, []) for mid in mode_ids}
    return values, {'algorithm': 'basket-ppr-v1', 'built_at': now, 'params': params,
        'ranking_fingerprint': hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()}


def _rank_pools(store, now, limit, *, listening=None):
    if listening is None:
        from yt_playlist.library import mix_listening
        listening = mix_listening.read(store, now)
    pools = {}
    for name, prepare, depth in (
            ("wheelhouse", surfaces.for_you, limit),
            ("explore", surfaces.explore_for_you, limit),
            ("comfort", surfaces.comfort_listening, limit),
            ("fresh", surfaces.cold_candidates, None)):
        with work_span("rec: pool " + name, level=logging.DEBUG):
            options = {} if name == 'comfort' else {'mix_listening': listening}
            pools[name] = prepare(store, now, limit=depth, **options)
    return pools


def _build_temporal(payload, mode_ids, C, lkeys, LV, cap, *, years, year_cuts,
                    suppressed, metadata, genres) -> None:
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
    allt = []
    for j, mid in enumerate(mode_ids):
        rows = np.where(near == j)[0]
        rows = rows[np.argsort(-sims[rows, j])]
        rows = [i for i in rows if lkeys[i] not in suppressed]
        items = [_temporal_item(lkeys[i], metadata, genres, years) for i in rows]
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


def _tilt_key(d, leans):
    """A light, vector-free transient score for ordering within a bucket: the track's genre lean
    multiplier (centered on 1.0). Higher sorts first."""
    fam = d.get("genre", "")
    return 1.0 + leans.get(f"genre:{fam}", 0.0)


def assemble_cards(store, now, epoch, *, avoid_genres=(), include_genres=(), mode_order=None,
                   preparation=None) -> list[dict]:
    """Select Home cards from the current recommendation publication."""
    preset = preparation['vibe_rank'].preset if preparation is not None else home_vibe.current(store, now)
    if preset:
        from yt_playlist.rec import vibe_serving
        return vibe_serving.assemble(store, now, preset, epoch,
                                   include_genres=include_genres, avoid_genres=avoid_genres)
    from yt_playlist.rec import stored_home
    return stored_home.assemble(store, now, epoch, avoid_genres=avoid_genres,
        include_genres=include_genres, mode_order=mode_order, preparation=preparation)


def assembly_steps(inputs, bundles, modes, chosen, epoch, n, leans, vibe_rank, *, cap_a, cap_al,
                   ab_share, comfort_minimum, include_genres=(), avoid_genres=()):
    """Detached card calculation, requesting only inputs needed by thin buckets.

    The caller acquires each requested page/fact on its pinned reader. No
    calculation receives a repository, and a full bucket never requests backfill.
    """
    meta = bundles.get("_meta", {})
    ppr_map = bundles.get("_ppr", {})

    # Resolve the 4th slot. Comfort needs a credible pool; otherwise the temporal card takes the slot
    # (when the library has enough year data), its band chosen by the epoch.
    comfort_ok = meta.get("comfort_pool", 0) >= comfort_minimum
    year_cuts = meta.get("year_cuts")
    band = epoch % 3
    fourth = "comfort" if comfort_ok else ("temporal" if year_cuts is not None else None)
    active = ["wheelhouse", "explore", "fresh"] + ([fourth] if fourth else [])

    def _bucket(mid, surf):
        b = list((bundles.get(str(mid)) or {}).get(surf, []))
        if surf == "temporal" and year_cuts is not None:
            b = [d for d in b if _in_band(d.get("year"), band, year_cuts[0], year_cuts[1])]
        return b

    blocked, muted = inputs['blocked'], inputs['muted']

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
    # Consider the whole row before spending a mode that may be a lane's only option.
    # At most six modes compete for four slots (360 assignments). Prefer credible
    # cards, then capped depth, then sampled priority in display order. Empty slots
    # let a smaller menu put its modes where tracks are available.
    from yt_playlist.rec.mode_selection import allocate_lanes
    best = allocate_lanes(chosen, active, depths, minimum=_MIN_CARD)
    assign = {surf: mid for surf, mid in zip(active, best) if mid is not None}

    # Songs already bundled into a generated playlist are spoken for, and must not be offered again
    # however stale the bundles are. The bundles apply this exclusion when they are BUILT, but
    # creating a playlist does not rebuild them, so every card between one build and the next handed
    # back exactly what you had just bundled: generate twice in a day and you got the same tracks.
    # Seeding `seen` makes it a render-time fact rather than a build-time snapshot, and reuses the
    # per-render machinery that already blocks a track across cards.
    cards, seen = [], set(blocked)
    full_pools = {}
    mode_families = {m["mode_id"]: max(m["families"], key=lambda f: f[1])[0]
                     for m in modes if m.get("families")}

    def _full_pool(surf):
        if surf not in full_pools:
            pool = yield 'pool', surf
            axes = yield 'axes', {d.get('genre') for d in pool}
            full_pools[surf] = [d for d in pool if d.get("artist") not in muted
                                and axes.get(d.get('genre'), ('', None))[0] not in avoid_genres]
        return full_pools[surf]

    def _card_for(surf, mid, ranker, ppr_pos):
        """Build one card for (surface, mode), or return (None, bucket) when it is too thin. Reads the
        running `seen` set from the enclosing scope."""
        bucket = _bucket(mid, surf)
        items = [d for d in bucket if d.get("key") and d["key"] not in seen
                 and d.get("artist") not in muted]
        candidate_keys = {d["key"] for d in items}
        order = _order_key(ranker, ppr_pos, leans)
        items.sort(key=order)
        items = vibe_rank(items)
        items = _diversify(items, cap_a, cap_al)
        if surf != "comfort" and len(items) < PROTO_SIZE:   # Comfort is NEVER backfilled (#63 credibility)
            taken = {d["key"] for d in items}
            pool = yield from _full_pool(surf)
            seen.update(blocked)
            if surf == "temporal" and year_cuts is not None:
                pool = [d for d in pool if _in_band(d.get("year"), band, year_cuts[0], year_cuts[1])]
            extra = [d for d in pool if d.get("key") and d["key"] not in seen and d["key"] not in taken]
            family = mode_families.get(mid)
            if family:
                axes = yield 'axes', {d.get('genre') for d in extra}
                distance = lambda d: genre_map.family_distance(family, axes.get(d.get('genre'), ('', None))[0])
                # Exhaust the SAME family across the whole library before adding close neighbors.
                # Never fill a soul mix with unrelated electronic tracks just because both were selected.
                extra = [d for d in extra if d.get("genre") and distance(d) <= 0.5]
                extra.sort(key=lambda d: (distance(d), order(d)))
            else:
                if include_genres:
                    axes = yield 'axes', {d.get('genre') for d in extra}
                    extra = home_themes.matching_values(extra, axes, include=include_genres)
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
        items = [{**item, "serving_context": {**item.get('serving_context', {}),
                  "score_kind": "candidate_fit", "source_ranker": surf,
                  "ordering_ranker": ranker, "ppr_position": ppr_pos.get(item["key"]) if ranker == "ppr" else None,
                  "facet_tilt": _tilt_key(item, leans), "vibe": vibe_rank.preset,
                  "vibe_affinity": vibe_rank.affinity(item)}} for item in items]
        return {"lane": surf, "label": label, "mode_id": mid, "ranker": ranker,
                "tracks": items, "_candidate_keys": candidate_keys}, bucket

    def _commit(card, bucket):
        # Replay the other ordering against this opportunity before the live card changes `seen`.
        # Nothing is persisted by previews; only the route's final rendered card becomes exposure.
        mid, surf = card["mode_id"], card["lane"]
        positions = {k: i for i, k in enumerate(ppr_map.get(str(mid), []))}
        decision = yield 'decision', None
        if positions and not (ab_share == 0 and decision.get("decision") == "retire"):
            other = "cosine" if card["ranker"] == "ppr" else "ppr"
            replay, _ = yield from _card_for(surf, mid, other, positions)
            if replay:
                candidates = sorted(card["_candidate_keys"] | replay["_candidate_keys"])
                policy = meta.get("ppr_policy", {"algorithm": "unknown-legacy-cache"})
                cohort_policy = {k: v for k, v in policy.items() if k != "built_at"}
                cohort = hashlib.sha256(json.dumps({"policy": cohort_policy, "share": ab_share,
                    "caps": [cap_a, cap_al], "vibe": vibe_rank.preset,
                    "leans": leans},
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
        card, bucket = yield from _card_for(surf, mid, ranker, ppr_pos)
        if card is not None:
            yield from _commit(card, bucket)
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
                tcard, tbucket = yield from _card_for("temporal", tmid, tranker, tpos)
                if tcard is not None:
                    yield from _commit(tcard, tbucket)
                    break
    return cards
