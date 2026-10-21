"""Trace the production serving policy and explicit responses without feeding them back as taste.

Only final track recommendations belong here, after assembly/diversity/rotation. Diagnostic previews,
search results and ordinary library tables are not recommendation impressions.
"""
import hashlib
import json
import logging
import uuid

from yt_playlist.rec import embed, rec_params, scoring, transient
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.repos.base import synchronized
from yt_playlist.util import genre_map
from yt_playlist.util.matching import normalize

log = logging.getLogger(__name__)
RETAIN_DAYS = 90


def _vector_identity(rows):
    digest = hashlib.sha256()
    for key, vector in sorted(rows):
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(vector)
    return digest.hexdigest()


@synchronized
def serving_policy(store, now, ranker="lane"):
    """Snapshot effective knobs, exclusions, time-dependent evidence and actual model contents.

    No credentials or unrelated application settings are included. Identical policy inputs have a
    stable identity; changing a preference or replacing vectors changes it even without a rebuild ID.
    """
    params = {spec.name: rec_params.get_param(store, spec.name) for spec in rec_params.PARAMS}
    taste = scoring.playlist_taste(store)
    content_taste = scoring.content_taste(store)
    taste_identity = hashlib.sha256(taste.centroids.tobytes() + taste.weights.tobytes()).hexdigest()
    configuration = {
        "schema": 2, "algorithm": "served-ranking-v2", "ranker": ranker,
        "params": params,
        "weights": store.get_weights(now=now, revert_halflife_d=params["weight_revert_halflife_d"]),
        "weight_records": store.weight_records(), "leans": store.get_leans(),
        "muted_artists": sorted(store.muted_artists()),
        "suppressed": sorted(store.suppressed_keys("for_you", now)),
        "facet_leans": transient.facet_leans(store, now),
        "mood": store.recent_mood_events(),
        "recent_plays": store.recent_plays_with_ts(limit=int(params["recent_play_limit"])),
        "evidence_plays": transient.evidence_plays(store, int(params["recent_play_limit"])),
        "recent_likes": store.recent_liked_with_ts(limit=int(params["recent_play_limit"])),
        "recent_skips": store.recent_skips_with_ts(now - 4 * params["skip_halflife_d"] * 86400),
        "radio_lists": transient.radio_list_ids(store),
        "home": {"vibe": store.get_setting("home_mix_vibe"),
                 "tuning_at": store.get_setting("home_mix_tuning_updated_at"),
                 "genre_request": store.get_proposals("home_genre_request")},
        "modes": {"definitions": [{k: v for k, v in m.items() if k != "centroid"}
                                   for m in store.modes.list_modes()],
                  "offered": store.modes.impression_counts(), "picks": store.modes.pick_rows()},
        "models": {"content_space": embed.content_space_id(store),
                   "collaborative": _vector_identity(store.get_rec_vectors()),
                   "content": _vector_identity(store.get_rec_content_vectors()),
                   "discovered_content": _vector_identity(store.get_rec_discovered_content_vectors()),
                   "artists": _vector_identity(store.get_rec_artist_vectors()),
                   "artist_content": _vector_identity(store.get_rec_artist_content_vectors()),
                   "taste": taste_identity,
                   "content_taste": hashlib.sha256(content_taste.centroids.tobytes() +
                                                    content_taste.weights.tobytes()).hexdigest(),
                   "baskets": hashlib.sha256(json.dumps(sorted(sorted(b) for b in store.rec_baskets()))
                                             .encode()).hexdigest(),
                   "modes": _vector_identity([(str(m["mode_id"]), m["centroid"].tobytes())
                                              for m in store.modes.list_modes()])},
    }
    raw = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16], configuration


@synchronized
def record_recommendations(store, surface, items, now, *, lane="", mode_id=None,
                           ranker="lane", provenance=None, request_id=None, experiment=None,
                           policy_kind="live", propensity=1.0):
    """Record final order with immutable candidate coverage, ownership, scores and policy."""
    items = [dict(it) if isinstance(it, dict) else vars(it).copy() for it in items]
    if not items:
        return
    # Instrumentation must never turn a successful recommendation response into an error.
    try:
        version, config = serving_policy(store, now, ranker)
        if experiment:
            config = {**config, "experiment": experiment, "schema": 3}
            version = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
        store.record_serving_policy(version, config, now)
        keys = [it.get("key") or it.get("identity_key") for it in items]
        meta = store.tracks_by_keys([k for k in keys if k])
        discovered = store.discovered_tracks_by_keys([k for k in keys if k])
        owned = RecDao(store).library_keys()
        content = RecDao(store).track_content()
        years = store.modes.years_for([k for k in keys if k])
        audio = RecDao(store).track_audio_features()
        last_plays = dict(store.recent_plays_with_ts())
        rows = []
        for rank, (key, item) in enumerate(zip(keys, items), 1):
            if not key:
                continue
            details = {**(meta.get(key) or {}), **(discovered.get(key) or {}), **item}
            genre = details.get("genre") or content.get(key, (None, None))[0]
            year = details.get("year") or years.get(key)
            candidate = {
                "artist": normalize(details.get("artist") or ""),
                "genre": genre_map.family(genre) if genre else None,
                "coverage": {"genre": bool(genre), "year": bool(year),
                             "audio": bool(details.get("audio") or audio.get(key) or
                                           any(details.get(c) not in (None, "") for c in store.rec.query.AUDIO_COLS))},
                "ownership": "owned" if key in owned else "cold" if key in discovered or
                             item.get("out_of_corpus") else "unknown",
                "last_played": last_plays.get(key), "video_id": details.get("video_id"),
            }
            rows.append({"identity_key": key, "rank": item.get("rank", rank),
                         "lane": item.get("lane") or lane, "mode_id": item.get("mode_id", mode_id),
                         "score": item.get("score"), "model_version": version,
                         "provenance": {"ranker": ranker, **(provenance or {}),
                                        **item.get("serving_context", {}), "candidate": candidate,
                                        "policy_kind": policy_kind, "propensity": propensity,
                                        "experiment": experiment,
                                        "pool_policy": item.get("pool_policy"),
                                        "score_available": item.get("score") is not None}})
        store.record_served_impressions(request_id or uuid.uuid4().hex, surface, rows, now,
                                        prune_before=now - RETAIN_DAYS * 86400)
    except Exception:
        log.warning("recommendation measurement failed for %s", surface, exc_info=True)


@synchronized
def stamp_pool(store, items, now, ranker="candidate-pool"):
    """Preserve the build policy on cached candidates; rendering may use newer preferences."""
    if not items:
        return items
    try:
        version, configuration = serving_policy(store, now, ranker)
        store.record_serving_policy(version, configuration, now)
        return [{**item, "pool_policy": version} for item in items]
    except Exception:
        log.warning("recommendation pool measurement failed for %s", ranker, exc_info=True)
        return items


@synchronized
def record_action(store, kind, now, *, keys=(), video_id=None, playlist_id=None,
                  event_id=None, generated=False):
    """Log a successful explicit action separately from observed plays and organic taste evidence."""
    try:
        keys = set(keys)
        if video_id:
            row = store.conn.execute("SELECT identity_key FROM tracks WHERE video_id=? LIMIT 1",
                                     (video_id,)).fetchone()
            if row:
                keys.add(row["identity_key"])
            else:
                keys.update(r["identity_key"] for r in store.get_discovered_tracks()
                            if r["video_id"] == video_id)
        if playlist_id is not None:
            keys.update(store.get_playlist_track_keys(playlist_id))
        store.record_recommendation_outcomes(event_id or uuid.uuid4().hex, kind, keys, now,
                                             generated=generated)
    except Exception:
        log.warning("recommendation response measurement failed for %s", kind, exc_info=True)
