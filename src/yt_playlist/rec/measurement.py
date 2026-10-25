"""Trace the production serving policy and explicit responses without feeding them back as taste.

Only final track recommendations belong here, after assembly/diversity/rotation. Diagnostic previews,
search results and ordinary library tables are not recommendation impressions.
"""
import hashlib
import json
import logging
import uuid
from copy import deepcopy

from yt_playlist.rec import choice_bandit, embedding_vectors as vectors, home_vibe, musical_character, rec_params, scoring, transient
from yt_playlist.core.activity import WorkClosing
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


class PoolPolicies:
    """One coherent policy calculation for a read-only candidate build, shared across its pools."""
    def __init__(self, store, now):
        self.store = store
        self.now = now
        self._configuration = None
        self.rows = {}

    def stamp(self, items, ranker):
        if not items:
            return items
        try:
            if self._configuration is None:
                _, self._configuration = serving_policy(self.store, self.now)
            configuration = {**self._configuration, "ranker": ranker}
            raw = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
            version = hashlib.sha256(raw.encode()).hexdigest()[:16]
            self.rows[version] = (version, raw, self.now)
            return [{**item, "pool_policy": version} for item in items]
        except WorkClosing:
            raise
        except Exception:
            log.warning("recommendation pool measurement failed for %s", ranker, exc_info=True)
            return items


def serving_policy(store, now, ranker="lane"):
    """Calculate a coherent fingerprint without holding the serving connection."""
    if getattr(store, "_read_only", False):
        return _serving_policy(store, now, ranker)
    with store.read_snapshot() as snapshot:
        return _serving_policy(snapshot.store, now, ranker)


def _serving_policy(store, now, ranker):
    """Snapshot effective knobs, exclusions, time-dependent evidence and actual model contents.

    No credentials or unrelated application settings are included. Identical policy inputs have a
    stable identity; changing a preference or replacing vectors changes it even without a rebuild ID.
    """
    params = {spec.name: rec_params.get_param(store, spec.name) for spec in rec_params.PARAMS}
    configuration = {
        "schema": 4, "algorithm": "served-ranking-v2", "ranker": ranker,
        "params": params,
        "weights": store.rec_model.get_weights(now=now, revert_halflife_d=params["weight_revert_halflife_d"]),
        "weight_records": store.rec_model.weight_records(), "leans": store.rec_model.get_leans(),
        "muted_artists": sorted(store.rec_model.muted_artists()),
        "suppressed": sorted(store.rec_model.suppressed_keys("for_you", now)),
        "facet_leans": transient.facet_leans(store, now),
        "mood": store.rec_signals.recent_mood_events(),
        "recent_plays": store.history.recent_plays_with_ts(limit=int(params["recent_play_limit"])),
        "evidence_plays": transient.evidence_plays(store, int(params["recent_play_limit"])),
        "recent_likes": store.rec_model.recent_liked_with_ts(limit=int(params["recent_play_limit"])),
        "recent_skips": store.player_events.recent_skips_with_ts(now - 4 * params["skip_halflife_d"] * 86400),
        "radio_lists": transient.radio_list_ids(store),
        "home": {"vibe": store.settings.get_setting("home_mix_vibe"),
                 "vibe_at": store.settings.get_setting('home_mix_vibe_updated_at'),
                 "tuning_at": store.settings.get_setting("home_mix_tuning_updated_at"),
                 "genre_request": store.rec_proposals.get_proposals("home_genre_request")},
        "modes": {"definitions": [{k: v for k, v in m.items() if k != "centroid"}
                                   for m in store.modes.list_modes()],
                  "offered": store.modes.impression_counts(), "picks": store.modes.pick_counts(),
                  "response_evidence": choice_bandit.stats(store, now)},
        "musical": {'profile_version': musical_character.VERSION, 'vibe_policy': home_vibe.VERSION},
        "models": store.memoized_read("serving-model-identities", lambda: _model_identities(store)),
    }
    raw = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16], configuration


def _model_identities(store):
    """Content identities, reusable only while all observed model/taste inputs are unchanged."""
    taste = scoring.playlist_taste(store)
    content_taste = scoring.content_taste(store)
    return {"content_space": vectors.content_space_id(store),
            "collaborative": _vector_identity(store.rec_model.get_rec_vectors()),
            "content": _vector_identity(store.rec_model.get_rec_content_vectors()),
            "discovered_content": _vector_identity(store.rec_model.get_rec_discovered_content_vectors()),
            "artists": _vector_identity(store.rec_model.get_rec_artist_vectors()),
            "artist_content": _vector_identity(store.rec_model.get_rec_artist_content_vectors()),
            "taste": hashlib.sha256(taste.centroids.tobytes() + taste.weights.tobytes()).hexdigest(),
            "content_taste": hashlib.sha256(content_taste.centroids.tobytes() +
                                             content_taste.weights.tobytes()).hexdigest(),
            "baskets": hashlib.sha256(json.dumps(sorted(sorted(b) for b in store.rec_evidence.rec_baskets()))
                                      .encode()).hexdigest(),
            "modes": _vector_identity([(str(m["mode_id"]), m["centroid"].tobytes())
                                       for m in store.modes.list_modes()])}


def record_recommendations(store, surface, items, now, *, lane="", mode_id=None,
                           ranker="lane", provenance=None, request_id=None, experiment=None,
                           policy_kind="live", propensity=1.0):
    """Record final order with immutable candidate coverage, ownership, scores and policy."""
    record_offers(store, surface, [{"items": items, "request_id": request_id,
        "lane": lane, "mode_id": mode_id, "ranker": ranker, "provenance": provenance,
        "experiment": experiment, "policy_kind": policy_kind, "propensity": propensity}], now)


def record_offers(store, surface, offers, now):
    """Capture one response's live cards and counterfactuals with one bounded queued reader."""
    # Instrumentation must never turn a successful recommendation response into an error.
    try:
        offers = [{**row, "items": [dict(it) if isinstance(it, dict) else vars(it).copy()
                                   for it in row["items"]]} for row in offers if row["items"]]
        if not offers:
            return
        first, *rest = offers
        offer = CapturedOffer(store, surface, now=now, **first)
        try:
            for row in rest:
                offer.add(**row)
        except BaseException:
            offer.close()
            raise
        submit = getattr(store, "background_measurement", None)
        if submit is not None:
            try:
                if not submit(offer):
                    offer.close()
                return
            except BaseException:
                offer.close()
                raise
        try:
            offer.run()
        finally:
            offer.close()
    except WorkClosing:
        raise
    except Exception:
        log.warning("recommendation measurement failed for %s", surface, exc_info=True)


class CapturedOffer:
    """Pin the actual offer's database state before deferring its expensive instrumentation.

    The worker bounds the number of open readers and closes every queued reader on shutdown.
    A later preference change must not rewrite the configuration attributed to an earlier offer.
    """
    def __init__(self, store, surface, items, now, *, request_id=None, **options):
        self.store, self.surface, self.now = store, surface, now
        self.items = deepcopy(items)
        self.options = deepcopy(options)
        self.request_id = request_id or uuid.uuid4().hex
        self.offers = [(self.items, self.request_id, self.options)]
        self._context = store.read_snapshot()
        self.snapshot = self._context.__enter__()

    def add(self, items, *, request_id=None, **options):
        self.offers.append((deepcopy(items), request_id or uuid.uuid4().hex, deepcopy(options)))

    @property
    def work_units(self):
        return len(self.offers)

    def close(self):
        if self._context is not None:
            context, self._context = self._context, None
            context.__exit__(None, None, None)

    def run(self):
        try:
            _, configuration = serving_policy(self.snapshot.store, self.now)
        except WorkClosing:
            raise
        except Exception:
            log.warning("recommendation measurement failed for %s", self.surface, exc_info=True)
            return
        for items, request_id, options in self.offers:
            try:
                version, config, rows = _prepare_impressions(
                    self.snapshot.store, items, self.now, configuration=configuration, **options)
                self.store.rec_ledger.record_serving_policy(version, config, self.now)
                self.store.rec_ledger.record_served_impressions(request_id, self.surface, rows,
                    self.now, prune_before=self.now - RETAIN_DAYS * 86400)
            except WorkClosing:
                raise
            except Exception:
                log.warning("recommendation measurement failed for %s", self.surface, exc_info=True)


def _prepare_impressions(store, items, now, *, lane="", mode_id=None, ranker="lane", provenance=None,
                         experiment=None, policy_kind="live", propensity=1.0, configuration=None):
    """Resolve policy and candidate metadata from the same detached snapshot."""
    if configuration is None:
        version, config = serving_policy(store, now, ranker)
    else:
        config = {**configuration, "ranker": ranker}
        raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
        version = hashlib.sha256(raw.encode()).hexdigest()[:16]
    if experiment:
        # Assignment and opportunity identities belong to the impression. The reusable policy
        # retains the experiment definition, including its cohort and actual ranker inputs.
        definition = {k: v for k, v in experiment.items() if k not in {"comparison_id", "cluster", "assignment"}}
        if experiment.get("assignment"):
            definition["assignment"] = {key: experiment["assignment"][key]
                                        for key in ("policy", "unit") if key in experiment["assignment"]}
        config = {**config, "experiment": definition}
        version = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    keys = [it.get("key") or it.get("identity_key") for it in items]
    offered = [key for key in keys if key]
    meta = store.rec_query.tracks_by_keys(offered)
    discovered = store.discovery.discovered_tracks_by_keys(offered)
    content = store.rec_query.content_inputs_by_keys(offered)
    last_plays = store.history.last_plays_for(offered)
    rows = []
    for rank, (key, item) in enumerate(zip(keys, items), 1):
        if not key:
            continue
        details = {**(meta.get(key) or {}), **(discovered.get(key) or {}), **item}
        inputs = content.get(key, {})
        genre = details.get("genre") or inputs.get("genre")
        year = details.get("year") or inputs.get("year")
        candidate = {
            "artist": normalize(details.get("artist") or ""),
            "genre": genre_map.family(genre) if genre else None,
            "coverage": {"genre": bool(genre), "year": bool(year),
                         "audio": bool(details.get("audio") or inputs.get("audio") or
                                       any(details.get(c) not in (None, "") for c in store.rec_query.AUDIO_COLS))},
            "ownership": "owned" if key in content else "cold" if key in discovered or
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
    return version, config, rows


def stamp_pool(store, items, now, ranker="candidate-pool"):
    """Preserve the build policy on cached candidates; rendering may use newer preferences."""
    if not items:
        return items
    try:
        with store.read_snapshot() as snapshot:
            policies = PoolPolicies(snapshot.store, now)
            stamped = policies.stamp(items, ranker)
        store.rec_ledger.record_prepared_policies(list(policies.rows.values()))
        return stamped
    except WorkClosing:
        raise
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
                keys.update(store.discovery.identity_keys_for_video(video_id))
        if playlist_id is not None:
            keys.update(store.playlists.get_playlist_track_keys(playlist_id))
        store.rec_ledger.record_recommendation_outcomes(event_id or uuid.uuid4().hex, kind, keys, now,
                                             generated=generated)
    except Exception:
        log.warning("recommendation response measurement failed for %s", kind, exc_info=True)
