"""Disposable Home collections. Preparation owns ranking; serving owns exposure.

The cache contains complete offers, not fitted models. Selection evidence stays in
the existing mode ledger and survives replacing or losing these prepared contents.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import logging
import math
import random
import time

from yt_playlist.core.activity import check_cancellable_work, work_budget, WorkBudgetExceeded
from yt_playlist.rec import (album_choices, choice_evidence, discover, fresh_pool, home_cards, home_themes, home_vibe,
                             mode_eval, mode_surfaces, rec_params, surfaces, taste_analysis,
                             transient, mix_tuning)
from yt_playlist.util import genre_map
from yt_playlist.rec.eligibility import home_exclusions

SURFACE = "home_prepared"
SCHEMA = 1
MAX_MENUS = 12
MAX_AGE_S = 1800
MAX_BYTES = 4_000_000
PREPARE_BUDGET_S = 2
_LOG = logging.getLogger(__name__)


class _NoJobs:
    def latest(self, kind, parameters):
        return None


def tuning_key(store, now=None):
    from yt_playlist.rec import candidate_refresh
    now = time.time() if now is None else now
    genres = store.rec_proposals.get_proposals("home_genre_request") or {}
    if genres.get("epoch") != home_cards._epoch(store, "cards"):
        genres = {}
    return _digest({"leans": store.rec_model.get_leans(),
                    "preferences": candidate_refresh.preferences(store, now),
                    "vibe": store.settings.get_setting(home_vibe.SETTING),
                    "album_feedback": store.settings.get_setting("album_feedback"),
                    "breadth": rec_params.get_param(store, "breadth_bias"),
                    "include": genres.get("include_genres", []),
                    "avoid": genres.get("avoid_genres", [])})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def prepare(store, now):
    """Prepare a bounded batch privately and publish only against unchanged inputs."""
    try:
        with work_budget(PREPARE_BUDGET_S):
            return _prepare(store, now)
    except WorkBudgetExceeded:
        store.maintenance_diagnostics.record('deferred', reasons=('time_budget',), phase='rec: Home collections')
        _LOG.debug('Home preparation deferred: total time budget exhausted; keeping previous collections')
        _LOG.debug('Home preparation budget boundary', exc_info=True)
        return None


def _prepare(store, now):
    started = time.monotonic()
    mix_tuning.expire(store, now)
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        target = "home_tuned" if mix_tuning.remaining(reader, now) > 0 else SURFACE
        previous = reader.rec_proposals.get_proposals(target)
        if not _valid(previous):
            previous = {}
        bundles = reader.rec_proposals.get_proposals('mode_bundles') or {}
        if (not bundles
                and not reader.rec_proposals.get_proposals('fresh_pool')
                and reader.rec_query.library_keys()):
            store.maintenance_diagnostics.record('deferred', reasons=('candidate_pools',),
                                                 phase='rec: Home collections')
            return None
        tuning = tuning_key(reader, now)
        # Fresh can publish before there is enough library evidence for modes. Its
        # candidates must retire old menus even when mode_bundles is still empty.
        generation = _digest([reader.rec_proposals._proposal_json(surface)
                              for surface in ('mode_bundles', 'fresh_pool')])
        reusable = (previous.get('generation') == generation and previous.get('tuning') == tuning
                    and now - previous.get('built_at', 0) < MAX_AGE_S)
        modes = ([] if bundles.get('_meta', {}).get('unmodeled')
                 else reader.modes.list_modes(active_only=True))
        mids = [m["mode_id"] for m in modes]
        selection_data = {"plays": reader.rec_query.play_counts(),
                          "audio": reader.rec_query.track_audio_features()}
        facets = reader.rec_query.play_weighted_facets(selection_data['plays'])
        options = home_themes.genre_options(reader, genre_plays=facets['genres'])
        fingerprint = {"breadth": taste_analysis.taste_breadth(reader, genre_plays=facets['genres']),
                       "eras": taste_analysis.era_distribution(reader, era_plays=facets['eras']),
                       "genre_plays": facets['genres'],
                       "transient": transient.facet_leans(reader, now)}
        collections = {"rediscover": surfaces.rediscover_playlists(reader, now, count=8, pool=8),
            "rediscover_albums": surfaces.rediscover_albums(reader, now, count=9, pool=9),
            "discovery_albums": discover.pick_discovered_albums(reader, 60, now, facet_leans=fingerprint['transient']),
            "discovery_artists": discover.pick_discovered_artists(reader, 40, now, facet_leans=fingerprint['transient'])}
        albums = reader.rec_proposals.get_proposals(album_choices.SURFACE) or {}
        collections["album_choices"] = albums.get('items', [])
        collections["album_choice_context"] = albums.get('context', {})
        collections["choice_context"] = choice_evidence.freeze_context(reader,
            choice_evidence.model_context(reader, now, facet_leans=fingerprint["transient"]))
        # These inputs belong to this snapshot, not to each recipe/menu. Re-reading
        # recent evidence for every card multiplied a small read into seconds of
        # repeated metadata queries during ordinary catch-up.
        selection_data['preparation'] = {'leans': fingerprint['transient'],
            'dists': (fingerprint['breadth']['families'], dict(fingerprint['eras'])),
            'vibe_rank': home_vibe.ranker(reader, now)}
        service = home_cards.HomeCards(reader, _NoJobs(), _LOG, genre_options=options,
                                      selection_data=selection_data)
        menus = {m['epoch']: m for m in previous.get('menus', [])} if reusable else {}
        cursor = previous.get('next_menu', 0) if reusable else 0
        # Cover different modes and temporal bands, including both ranker allocations,
        # rather than predetermining the next several choices using today's feedback.
        for index in range(MAX_MENUS if mids else 1):
            check_cancellable_work()
            offset = (cursor + index) % MAX_MENUS
            start = (offset // 3 + (offset % 3) * 4) % len(mids) if mids else 0
            order = tuple(mids[start:] + mids[:start]) if mids else None
            result = service.cards(home_cards.CardsRequest(now, epoch=offset,
                                                          mode_order=order, record=False))
            menu = asdict(result)
            for proto in menu["protos"]:
                proto["experiment_id"] = home_cards.experiment_id(proto)
                proto.setdefault("recipe", {})
                proto["recipe"]["experiment_id"] = proto["experiment_id"]
            # Slots must reference the same final offers that carry experiment attribution.
            by_lane = {p["lane"]: p for p in menu["protos"]}
            for slot in menu["slots"]:
                if slot["proto"]:
                    slot["proto"] = by_lane[slot["proto"]["lane"]]
            menus[offset] = menu
            # Reserve publication time; never insist on three menus after an
            # expensive setup. Each successful batch is immediately usable.
            if time.monotonic() - started >= PREPARE_BUDGET_S * 0.5:
                break
        payload = {"schema": SCHEMA, "built_at": now, "tuning": tuning,
            "generation": generation, "next_menu": (offset + 1) % MAX_MENUS,
            "menus": [menus[k] for k in sorted(menus)], "genre_options": options,
            "modes": [{**m, "centroid": list(map(float, m["centroid"]))} for m in modes],
            "play_exposure": transient.play_facet_leans(reader, now),
            "fingerprint": fingerprint, **collections,
            "discovery_tracks": fresh_pool.pick(reader, now, size=40, minimum=1)}
        encoded = json.dumps(payload, separators=(",", ":"))
        if len(encoded.encode()) > MAX_BYTES:
            raise ValueError("Prepared Home collections exceeded their size budget")
        check_cancellable_work()
        if not snapshot.publish(lambda: store.rec_proposals.publish_prepared_home(encoded, now, surface=target),
                                allow_stale_tables=mode_surfaces.RANKING_CHURN_TABLES):
            return None
        return payload


def load(store, now, schedule=lambda: None):
    """A missing/corrupt cache never falls back to synchronous ranking."""
    mix_tuning.expire(store, now)
    key = tuning_key(store, now)
    for surface in ("home_tuned", SURFACE):
        try:
            payload = store.rec_proposals.get_proposals(surface)
            if not _valid(payload):
                continue
            if surface == "home_tuned" and payload.get("tuning") != key:
                continue
            if now - payload["built_at"] >= MAX_AGE_S or payload.get("tuning") != key:
                schedule()
            return payload
        except (ValueError, TypeError, KeyError):
            # A damaged tailored entry must not hide a healthy baseline collection.
            continue
    schedule()
    return None


def _valid(payload):
    def number(value):
        return isinstance(value, (int, float)) and math.isfinite(value)

    def pairs(value):
        return isinstance(value, list) and all(isinstance(p, (list, tuple)) and len(p) == 2
            and isinstance(p[0], str) and number(p[1]) for p in value)

    def track(value):
        return (isinstance(value, dict)
                and all(isinstance(value.get(k), str) for k in ('key', 'title', 'artist', 'video_id'))
                and isinstance(value.get('genre', ''), str))

    def recipe(value):
        if not isinstance(value, dict):
            return False
        genres = value.get('genre_request') or {}
        return isinstance(genres, dict) and all(isinstance(genres.get(k, []), list)
            and all(isinstance(g, str) for g in genres.get(k, [])) for k in ('include', 'avoid'))

    def proto(value):
        return (isinstance(value, dict) and value.get('lane') in (*mode_surfaces.CARD_SURFACES, 'temporal')
                and all(isinstance(value.get(k), str) for k in ('experiment_id', 'name', 'label', 'note'))
                and recipe(value.get('recipe')) and isinstance(value.get('tracks'), list)
                and all(track(t) for t in value['tracks'])
                and (value.get('experiment') is None or isinstance(value['experiment'], dict))
                and (value.get('counterfactual') is None or
                     (isinstance(value['counterfactual'], dict)
                      and isinstance(value['counterfactual'].get('tracks'), list)
                      and all(track(t) for t in value['counterfactual']['tracks']))))

    try:
        if (not isinstance(payload, dict) or payload["schema"] != SCHEMA
                or not isinstance(payload["fingerprint"], dict)
                or not number(payload["built_at"])
                or not isinstance(payload.get('next_menu', 0), int)
                or not 0 <= payload.get('next_menu', 0) < MAX_MENUS
                or not isinstance(payload["genre_options"], list)
                or not isinstance(payload["menus"], list)
                or not 0 < len(payload["menus"]) <= MAX_MENUS):
            return False
        if any(not isinstance(g, dict) or not all(isinstance(g.get(k), str) for k in ('family', 'label'))
               for g in payload['genre_options']):
            return False
        for menu in payload["menus"]:
            if (not isinstance(menu["epoch"], int) or not isinstance(menu["protos"], list)
                    or not isinstance(menu["slots"], list)):
                return False
            for field in ("genre_notice", "genre_warning", "requested_genres"):
                if field not in menu:
                    return False
            if (any(menu[field] is not None and not isinstance(menu[field], str)
                    for field in ('genre_notice', 'genre_warning'))
                    or not isinstance(menu['requested_genres'], list)
                    or not all(isinstance(g, str) for g in menu['requested_genres'])):
                return False
            if not all(proto(p) for p in menu['protos']):
                return False
            for slot in menu["slots"]:
                if slot['proto'] is not None:
                    if not proto(slot['proto']):
                        return False
                elif (not isinstance(slot['pool'], dict)
                      or not isinstance(slot['pool'].get('parameters'), dict)
                      or not isinstance(slot['pool'].get('lane'), str)):
                    return False
        if (not isinstance(payload["fingerprint"]["breadth"]["families"], dict)
                or not isinstance(payload["fingerprint"]["transient"], dict)
                or not pairs(payload["fingerprint"]["eras"])):
            return False
        if any(not isinstance(k, str) or not number(v) for field in
               (payload['fingerprint']['breadth']['families'], payload['fingerprint']['transient'])
               for k, v in field.items()):
            return False
        for field, identity in (('rediscover', 'id'), ('rediscover_albums', 'browse_id'),
                                ('discovery_albums', 'browse_id'), ('discovery_artists', 'artist_id')):
            if not isinstance(payload.get(field), list) or any(
                    not isinstance(value, dict) or identity not in value for value in payload[field]):
                return False
        if not isinstance(payload.get('discovery_tracks'), list) or not all(track(t) for t in payload['discovery_tracks']):
            return False
        if not isinstance(payload.get('modes', []), list) or any(
                not isinstance(m, dict) or not isinstance(m.get('mode_id'), int)
                or not number(m.get('size')) or not pairs(m.get('families', []))
                for m in payload.get('modes', [])):
            return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def remember(store, payload, result, now):
    """Reuse the user's explicit tailored result on subsequent ordinary reads."""
    # Pool slots may contain a live job with locks; only the completed offers are cached.
    menu = asdict(home_cards.CardsResult(result.protos, [], result.genre_notice, result.genre_warning,
                                       result.genre_options, result.requested_genres, result.epoch, result.vibe))
    for proto in menu["protos"]:
        proto["experiment_id"] = home_cards.experiment_id(proto)
        proto["recipe"]["experiment_id"] = proto["experiment_id"]
    by_lane = {p["lane"]: p for p in menu["protos"]}
    menu["slots"] = []
    for slot in result.slots:
        if slot["proto"]:
            menu["slots"].append({"proto": by_lane[slot["proto"]["lane"]], "pool": None})
        elif slot["pool"]:
            # A live job remains in the job registry, never in this restartable cache.
            menu["slots"].append({"proto": None,
                "pool": {**slot["pool"], "job": None, "building": False}})
    tailored = {**(payload or {}), "schema": SCHEMA, "built_at": now,
                "tuning": tuning_key(store, now), "menus": [menu],
                "genre_options": result.genre_options,
                "fingerprint": (payload or {}).get("fingerprint", {
                    "breadth": {"families": {}, "breadth": 0}, "eras": [], "transient": {}})}
    for field in ('rediscover', 'rediscover_albums', 'discovery_albums', 'discovery_artists', 'discovery_tracks'):
        tailored.setdefault(field, [])
    # An explicit tuning/build replaces the displayed menu even within the same
    # choice window. Preserve that choice across refills without attributing the
    # replacement to the earlier randomized menu assignment.
    held = {"epoch": result.epoch, "tuning": tailored["tuning"],
            "experiments": [p["experiment_id"] for p in menu["protos"]],
            "menu": menu, "choice_context": (result.choice_context if result.choice_context is not None
                                              else tailored.get("choice_context")),
            "assignment": None}
    store.rec_proposals.remember_home(json.dumps(tailored, separators=(",", ":")),
                                      json.dumps(held, separators=(",", ":")), now)


def _eligible_protos(store, protos, now):
    """Bounded current checks over the prepared offer, without a catalog ranking pass."""
    protos = deepcopy(protos)
    keys = list({t["key"] for p in protos
                 for t in [*p["tracks"], *(p.get("counterfactual") or {}).get("tracks", [])]})
    owned = store.rec_query.tracks_by_keys(keys)
    discovered = store.discovery.discovered_tracks_by_keys(keys, available_only=True)
    blocked = home_exclusions(store, keys, now)
    muted = store.rec_model.muted_artists()
    genres = store.rec_query.track_genres(keys)
    kept = []
    for proto in protos:
        def eligible(track):
            key = track["key"]
            current = owned.get(key) or discovered.get(key)
            if not current or key in blocked or current.get("artist") in muted:
                return False
            if proto["lane"] != "fresh" and key not in owned:
                return False
            if proto["lane"] == "fresh" and key in owned:
                return False
            request = proto.get("recipe", {}).get("genre_request") or {}
            family = genre_map.family(genres.get(key) or current.get("genre") or track.get("genre"))
            if family in request.get("avoid", []) or (request.get("include") and family not in request["include"]):
                return False
            if track.get("genre") and family != genre_map.family(track["genre"]):
                # A corrected genre needs a newly prepared description/mode attribution.
                return False
            track.update({k: current[k] for k in ("title", "artist", "video_id", "thumbnail") if k in current})
            return True
        proto["tracks"] = [t for t in proto["tracks"] if eligible(t)]
        if len(proto["tracks"]) < 4:
            continue
        if shadow := proto.get("counterfactual"):
            shadow["tracks"] = [t for t in shadow["tracks"] if eligible(t)]
        kept.append(proto)
    return kept


def cards(store, payload, now, service, schedule=lambda: None, *, record=True):
    """Choose an experiment from prepared alternatives using current mode evidence."""
    epoch = home_cards._epoch(store, "cards")
    tuning = tuning_key(store, now)
    genres = store.rec_proposals.get_proposals("home_genre_request") or {}
    constrained = genres.get("epoch") == epoch and (genres.get("include_genres") or genres.get("avoid_genres"))
    if not payload or (constrained and payload.get("tuning") != tuning):
        schedule()
        return home_cards.CardsResult([], [], None, None,
            (payload or {}).get("genre_options", []), [], epoch, home_vibe.current(store, now))
    menus = payload["menus"]
    band = epoch % 3
    candidates = [m for m in menus if m["epoch"] % 3 == band] or menus
    # Each menu was already diversified and ordered. This draw is bounded by the
    # batch size, and can react to feedback without recomputing a single track score.
    try:
        held = store.rec_proposals.get_proposals("home_choice") or {}
        if not isinstance(held, dict) or not isinstance(held.get("experiments"), list):
            held = {}
    except (TypeError, ValueError):
        held = {}
    if held.get("epoch") == epoch and held.get("tuning") == tuning:
        chosen = held.get("menu")
        if not chosen:
            identities = set(held["experiments"])
            chosen = max(candidates, key=lambda m: sum(p["experiment_id"] in identities for p in m["protos"]))
            # Legacy held records did not freeze their menu. Its replacement has no
            # known probability, even when some experiment IDs still match.
            held["assignment"] = None
    else:
        # Only a new choice window reads historical evidence. The displayed choice
        # survives refills; new feedback influences the next window.
        started = store.modes.epoch_started_at(epoch)
        stats = mode_eval.mode_bandit_stats(store, before=started, now=now)
        modes = payload.get("modes", [])
        priorities = {m["mode_id"]: (lambda ab: ab[0] / sum(ab))(
            mode_eval.choice_posterior(*stats.get(m["mode_id"], (0, 0)))) for m in modes}
        context = (payload.get("fingerprint") or {}).get("transient", {})
        size_power = rec_params.get_param(store, "bandit_size_power")
        priorities = {m["mode_id"]: priorities[m["mode_id"]] * max(1, m["size"]) ** size_power
                      * mode_surfaces._mode_mood_weight(m, context) for m in modes}
        def score(menu):
            mids = {p.get("mode_id") for p in menu["protos"] if p.get("mode_id") is not None}
            return sum(priorities.get(mid, 0.5) for mid in mids) / max(1, len(mids))
        values = [max(0.001, score(menu)) for menu in candidates]
        exploration = rec_params.get_param(store, "bandit_exploration")
        probabilities = [(1 - exploration) * value / sum(values) + exploration / len(values)
                         for value in values]
        rng = random.SystemRandom() if record else random.Random(epoch)
        index = rng.choices(range(len(candidates)), weights=probabilities, k=1)[0]
        chosen = candidates[index]
        held = {"choice_context": payload.get('choice_context'),
                "assignment": {"policy": "prepared-menu-categorical-v1", "unit": "whole_menu",
                "menu": chosen["epoch"], "probability": probabilities[index],
                "candidates": [{"menu": menu["epoch"], "probability": probability}
                               for menu, probability in zip(candidates, probabilities)],
                "generation": payload.get("generation"), "at": now}}
    protos = _eligible_protos(store, chosen["protos"], now)
    if (len(protos) != len(chosen["protos"])
            or sum(len(p["tracks"]) for p in protos) != sum(len(p["tracks"]) for p in chosen["protos"])):
        schedule()
        if held.get("assignment"):
            held["assignment"] = {**held["assignment"], "censored": True,
                                  "reason": "eligibility_changed_after_assignment"}
    for proto in protos:
        proto["assignment"] = held.get("assignment")
        if proto.get("experiment"):
            proto["experiment"]["cluster"] = f"{epoch}:{proto.get('mode_id')}"
            proto["experiment"]["assignment"] = held.get("assignment")
            # Randomizing whole menus does not isolate an individual ranker's effect.
            proto["experiment"]["ranker_controlled"] = False
    by_lane = {p["lane"]: p for p in protos}
    slots = []
    for slot in chosen["slots"]:
        if slot["proto"]:
            if proto := by_lane.get(slot["proto"]["lane"]):
                slots.append({"proto": proto, "pool": None})
        elif slot["pool"]:
            pool = deepcopy(slot["pool"])
            if "epoch" in pool["parameters"]:
                pool["parameters"]["epoch"] = epoch
            kind = "fresh_pool" if pool["lane"] == "fresh" else "library_pool"
            if job := service.jobs.latest(kind, pool["parameters"]):
                pool = service.pool_view(pool["parameters"], job, genre_options=payload["genre_options"])
            slots.append({"proto": None, "pool": pool})
    result = home_cards.CardsResult(protos, slots,
        chosen["genre_notice"], chosen["genre_warning"], payload["genre_options"],
        chosen["requested_genres"], epoch, home_vibe.current(store, now),
        choice_context=held.get('choice_context') or {})
    if record and protos:
        if held.get("epoch") != epoch or held.get("tuning") != tuning:
            store.rec_proposals.put_proposals("home_choice", {"epoch": epoch, "tuning": tuning,
                "experiments": [p["experiment_id"] for p in protos],
                "menu": chosen,
                "choice_context": held.get('choice_context'),
                "assignment": held.get("assignment")}, now)
        service.record_modes(protos, epoch, now)
        service.record_offer(protos, now, epoch=epoch)
    return result


def fingerprint(store, payload, now):
    empty = {"breadth": {"families": {}, "breadth": 0}, "eras": [], "transient": {}}
    return taste_analysis.taste_fingerprint(store, now, prepared=(payload or {}).get("fingerprint", empty))


def collections(store, payload, now):
    """Rotate small prepared pools, retaining current source and feedback eligibility."""
    payload = payload or {}
    epoch = home_cards._epoch(store, "rediscover")
    playlists = [{**p, **current} for p in payload.get('rediscover', [])
                 if (current := store.playlists.home_preview(p['id']))]
    saved = store.collection.saved_album_ids(browse_ids=[a['browse_id'] for a in payload.get('rediscover_albums', [])])
    albums = [a for a in payload.get("rediscover_albums", []) if a["browse_id"] in saved]
    return {"rediscover": surfaces.rotate_page(playlists, 2, epoch),
            "rediscover_albums": surfaces.rotate_page(albums, 3, epoch),
            **discovery(store, payload, now)}


def discovery(store, payload, now):
    payload = payload or {}
    epoch = home_cards._epoch(store, "discover")
    muted = store.rec_model.muted_artists()
    weights = store.rec_model.get_weights(now=now)
    def visible(item):
        family = genre_map.family(item.get("genre"))
        subgenre = genre_map.subgenre(item.get("genre") or "")
        return (item.get("artist") not in muted and weights.get(f"genre:{family}", 1) != 0
                and weights.get(f"genre:{subgenre}", 1) != 0)
    album_pool, artist_pool = payload.get("discovery_albums", []), payload.get("discovery_artists", [])
    album_ids, artist_ids = store.discovery.present_home_tiles(
        [a["browse_id"] for a in album_pool], [a["artist_id"] for a in artist_pool])
    saved = store.collection.saved_album_ids(browse_ids=[a['browse_id'] for a in album_pool])
    albums = [{**a, **album_ids[a["browse_id"]]} for a in album_pool
              if a["browse_id"] in album_ids and a["browse_id"] not in saved]
    artists = [{**a, **artist_ids[a["artist_id"]]} for a in artist_pool if a["artist_id"] in artist_ids]
    albums = [a for a in albums if visible(a)]
    artists = [a for a in artists if visible(a)]
    tracks = payload.get("discovery_tracks", [])
    # Reuse bounded offer eligibility, allowing discovery grids smaller than a mix.
    if tracks:
        keys = [t["key"] for t in tracks]
        owned = store.rec_query.tracks_by_keys(keys)
        present = store.discovery.discovered_tracks_by_keys(keys, available_only=True)
        blocked = store.rec_model.suppressed_keys("for_you", now, keys=keys)
        tracks = [{**t, **{k: present[t["key"]].get(k) for k in
                          ("title", "artist", "video_id", "thumbnail", "genre")}}
                  for t in tracks if t["key"] in present and t["key"] not in owned and t["key"] not in blocked]
        tracks = [t for t in tracks if visible(t)]
    return {"discovery_albums": surfaces.rotate_page(albums, 15, epoch),
            "discovery_artists": surfaces.rotate_page(artists, 10, epoch),
            "discovery_tracks": surfaces.rotate_page(tracks, 10, epoch)}
