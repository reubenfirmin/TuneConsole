"""Place changed recordings in the saved content space and prepared cluster pools.

Enrichment changes a few feature vectors, not the learned encoding or the co-listen graph.
Keep the centroids and PageRank rankings; never fit a model or rank the library here.
"""
import hashlib
import json
import logging

import numpy as np

from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec import embed, rec_params, taste_modes
from yt_playlist.util import genre_map

log = logging.getLogger(__name__)
_BATCH_SIZE = 128
_LOCAL_LIMIT = 96
_SURFACES = ("wheelhouse", "explore", "comfort", "fresh", "temporal")


@work_span("rec: incremental content update", announce=True)
def update_tracks(store, keys, now):
    keys = list(dict.fromkeys(keys))
    placed = 0
    for offset in range(0, len(keys), _BATCH_SIZE):
        batch = keys[offset:offset + _BATCH_SIZE]
        for _ in range(3):
            with store._lock:
                raw = store.settings.get_setting("rec_content_model")
                generation = store.settings.get_setting("rec_content_generation") or ""
                if not raw:
                    # Initial training belongs to model initialization, not a metadata batch.
                    log.info("rec incremental: %d changed tracks deferred until model initialization", len(batch))
                    return placed
                library = store.rec_query.content_inputs_by_keys(batch)
                discovered = store.discovery.discovered_tracks_by_keys(batch)
                meta = store.rec_query.tracks_by_keys(batch)
            model = json.loads(raw)
            lv = _encode(model, library)
            dv = _encode(model, discovered)
            with store._lock:
                if not store.rec_model.update_content_vectors(
                        {key: lv[key].tobytes() if key in lv else None for key in batch},
                        {key: dv[key].tobytes() if key in dv else None for key in batch},
                        expected_generation=generation):
                    continue
                modes = taste_modes.live_modes(store)
                payload = store.rec_surface.get_proposals("mode_bundles") or {}
                fresh = store.rec_surface.get_proposals("fresh_pool")
                _update_pools(store, batch, library, discovered, meta, lv, dv, modes, payload, fresh, raw, now)
                placed += len(lv.keys() | dv.keys())
                break
        else:
            raise RuntimeError("Content model changed during incremental placement; retry the changed tracks")
    log.info("rec incremental: requested=%d encoded=%d; reused saved centroids and PageRank", len(keys), placed)
    return placed


def _encode(model, rows):
    vectors = {}
    for key, row in rows.items():
        vector = embed.encode_content(model, row.get("genre"), row.get("year"), row.get("audio"))
        if vector is not None:
            vectors[key] = vector
    return vectors


def _update_pools(store, keys, library, discovered, meta, lv, dv, modes, payload, fresh, raw, now):
    blocked = store.rec_query.suppressed_candidate_keys(keys, now)
    muted = {a.casefold() for a in store.rec_model.muted_artists()}
    weights = store.rec_model.get_weights(now=now)
    details, membership = {}, {}
    vectors = {**dv, **lv}
    for key in keys:
        row = ({**library[key], **meta[key]} if key in meta and key in library else discovered.get(key))
        if not row or not row.get("video_id") or key in blocked or (row.get("artist") or "").casefold() in muted:
            continue
        genre = row.get("genre") or ""
        if any(weights.get(f"genre:{g}", 1.0) == 0 for g in (genre_map.family(genre), genre_map.subgenre(genre)) if g):
            continue
        year = str(row.get("year") or "")[:4]
        details[key] = {"key": key, **{name: row.get(name) for name in
            ("video_id", "title", "artist", "album", "thumbnail")}, "genre": genre,
            "year": int(year) if year.isdigit() else None}
        vector = vectors.get(key)
        if vector is not None and modes:
            nearest = max(modes, key=lambda mode: float(np.einsum(
                "i,i->", vector, mode["centroid"], optimize=False)))
            membership[key] = str(nearest["mode_id"])

    changed = set(keys)

    def refresh(rows, surface, mid=None):
        result = []
        for old in rows:
            key = old.get("key")
            if key not in changed:
                result.append(old)
                continue
            is_fresh = surface == "fresh"
            eligible = key in details and ((key in discovered and key not in library) if is_fresh else key in meta)
            if surface == "temporal":
                eligible &= key in lv
            if not eligible or (mid is not None and membership.get(key) != mid):
                continue
            result.append({**old, **details[key]})
        return result

    all_pools = payload.setdefault("all", {}) if payload else {}
    new_policy = {"algorithm": "content-local-insert-v1",
                  "content_model": hashlib.sha256(raw.encode()).hexdigest(),
                  "parents": sorted({item["pool_policy"] for surface in _SURFACES
                      for item in all_pools.get(surface, []) if item.get("pool_policy")})}
    policy_id = hashlib.sha256(json.dumps(new_policy, sort_keys=True).encode()).hexdigest()[:16]
    inserted = False
    cap = int(rec_params.get_param(store, "modes_cand_per_mode"))
    for surface in _SURFACES:
        rows = refresh(all_pools.get(surface, []), surface)
        present = {item.get("key") for item in rows}
        # Existing ranked lanes keep their order and scores. Newly encoded recordings enter the
        # membership lane (library) or the metadata-only cold lane, without inventing a taste score.
        if surface in ("temporal", "fresh"):
            for key, detail in details.items():
                eligible = (key in meta and key in lv) if surface == "temporal" else (
                    key in discovered and key not in library and (key in dv or detail["genre"] or detail["year"]))
                if key not in present and eligible:
                    rows.append({**detail, "plays": 0, "reason": "", "lane": "cold" if surface == "fresh" else "temporal",
                                 "score": None, "pool_policy": policy_id})
                    inserted = True
        all_pools[surface] = rows
        candidates = {item["key"]: item for item in rows if item.get("key") in changed}
        for mode in modes:
            mid = str(mode["mode_id"])
            if mid not in payload:
                continue
            bucket = payload[mid]
            old_local = bucket.get(surface, [])
            if not (any(item.get("key") in changed for item in old_local)
                    or any(membership.get(key) == mid for key in candidates)):
                continue
            local = refresh(old_local, surface, mid)
            present = {item.get("key") for item in local}
            for key, item in candidates.items():
                if membership.get(key) == mid and key not in present:
                    _insert_local(store, local, item, vectors[key])
            if surface == "temporal" and (cuts := payload.get("_meta", {}).get("year_cuts")):
                # Preserve the existing per-era capacity when a recording's release year changes.
                bands = [[], [], []]
                for item in local:
                    if item.get("year") is not None:
                        band = 0 if item["year"] <= cuts[0] else (1 if item["year"] <= cuts[1] else 2)
                        bands[band].append(item)
                local = [item for band in bands for item in band[:cap]]
            else:
                local = local[:cap]
            bucket[surface] = local
    if inserted:
        store.rec_surface.record_serving_policy(policy_id, new_policy, now)
    if payload:
        # _ppr, centroids, model parameters and all unaffected candidate orders stay intact.
        store.rec_surface.put_proposals("mode_bundles", payload, now)
    if fresh is not None or discovered:
        updated = refresh(fresh or [], "fresh")
        present = {item.get("key") for item in updated}
        updated.extend(item for item in all_pools.get("fresh", []) if item.get("key") in changed and item["key"] not in present)
        store.rec_surface.put_proposals("fresh_pool", updated, now)


def _insert_local(store, pool, item, vector):
    """Find one position among the destination cluster's cached neighbours only."""
    neighbours = pool[:_LOCAL_LIMIT]
    if not neighbours:
        pool.append(item)
        return
    local = {key: np.frombuffer(blob, dtype=np.float32) for key, blob in
             store.rec_model.content_vectors_by_keys(row["key"] for row in neighbours).items()}
    scores = [(i, float(np.einsum("i,i->", vector, local[row["key"]], optimize=False)))
              for i, row in enumerate(neighbours) if row["key"] in local]
    position = max(scores, key=lambda pair: pair[1])[0] + 1 if scores else len(pool)
    pool.insert(position, item)
