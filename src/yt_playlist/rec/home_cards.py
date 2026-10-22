"""Home recommendation application service, independent of HTTP and templates.

The existing recommendation engines own ranking. This module assembles their final
offers and owns discovery counts, mode/served impressions and diagnostic auditing.
Job admission stays behind the small PoolJobs port; the web adapter owns execution.
"""
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from typing import Any, Protocol
import uuid

from yt_playlist.rec import (arc_energy, home_audit, home_themes, home_vibe, journeys,
                             measurement, mix_tuning, ordering, rec_params, recipes, surfaces)
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.rec_surface import RecSurfaceRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.util import genre_map

PROTO_SIZE = 12
# A deep pool keeps refreshes varied; existing surfaces score the whole model regardless of limit.
ROTATION_POOL = PROTO_SIZE * 25
_ROW_LANES = ("wheelhouse", "explore", "comfort", "fresh")
_CARD_LABELS = {"wheelhouse": "More in your wheelhouse", "explore": "From your catalog",
                "comfort": "Comfort listening", "fresh": "Fresh songs"}


class HomeRepositories(Protocol):
    discovery: DiscoveryRepo
    modes: ModesRepo
    rec_model: RecModelRepo
    rec_query: RecQueryRepo
    rec_surface: RecSurfaceRepo
    settings: SettingsRepo


class PoolJob(Protocol):
    kind: str
    parameters: dict
    exclude_keys: list[str]
    done: bool
    error: str | None
    events: Any


class PoolJobs(Protocol):
    def latest(self, kind: str, parameters: dict) -> PoolJob | None: ...
    def start(self, kind: str, parameters: dict, excluded: list[str]) -> PoolJob: ...


@dataclass(frozen=True)
class CardsRequest:
    now: float
    regenerating: bool = False
    use_genre_request: bool = True
    genre_notice: str | None = None


@dataclass
class CardsResult:
    protos: list[dict]
    slots: list[dict]
    genre_notice: str | None
    genre_warning: str | None
    genre_options: list[dict]
    requested_genres: list[str]
    epoch: int
    vibe: str | None


@dataclass
class PoolResult:
    proto: dict | None = None
    pool: dict | None = None


def _proto(store, lane, label, items, now):
    when = datetime.fromtimestamp(now).strftime("%B %-d %Y")
    tracks = items[:PROTO_SIZE]
    genres, _ = recipes.theme_counts(store, tracks)
    return {"lane": lane, "label": label, "name": f"{label} - {when}",
            "note": recipes.theme_sentence(store, lane, tracks), "tracks": tracks,
            "genres": list(genres), "feedback_surface": "for_you" if lane == "fresh" else None}


def _order_by_journey(store, items, journey, seed):
    items = ordering.attach_genres(store, items)
    field = ordering._field
    keys = [field(it, "key") or "" for it in items]
    dao = store.rec_query
    decades, lastp, plays = dao.track_decades(keys), dao.track_last_played(keys), dao.play_counts()
    genres = {(field(it, "key") or ""): (field(it, "genre") or "") for it in items}
    arc = arc_energy.arc_energies(keys, genres, dao.track_audio_features())

    def feat(it):
        key, genre = field(it, "key") or "", field(it, "genre") or ""
        return {"artist": field(it, "artist") or "", "genre": genre,
                "energy": arc.get(key, genre_map.energy(genre)), "decade": decades.get(key),
                "plays": plays.get(key, 0), "recency": lastp.get(key, 0.0)}

    return journeys.journey_order(items, journey, seed, feat)


def _epoch(store, card):
    cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
    return max(0, store.rec_surface.card_views(card) - 1) // cap


def _rolled_theme(store, lane, pool, now):
    dists = recipes.pool_facets(store, pool)
    avoid = {"genres": set(), "eras": set()}
    for other in _ROW_LANES:
        this_one = other == lane
        recipe = recipes.roll_recipe(store, other, seed=f"{other}:{_epoch(store, other)}",
                                     now=now, avoid=avoid, dists=dists if this_one else None)
        if this_one:
            return recipe
        avoid["genres"] |= set(recipe.get("facets", {}).get("genres", []))
        avoid["eras"] |= set(recipe.get("facets", {}).get("eras", []))
    return recipes.roll_recipe(store, lane, seed=f"{lane}:{_epoch(store, lane)}", now=now, dists=dists)


def _carded(store, lane, label, pool, now):
    pool = ordering.attach_genres(store, list(pool))
    recipe = _rolled_theme(store, lane, pool, now)
    focused = recipes.theme_filter(store, pool, recipe.get("facets", {}))
    focused = home_vibe.rank(store, now, focused)
    items = (focused[:PROTO_SIZE] if home_vibe.current(store, now) else
             surfaces.rotate_sample(focused[:PROTO_SIZE * 2], PROTO_SIZE, _epoch(store, lane)))
    items = _order_by_journey(store, items, recipe.get("journey", "shuffle"), recipe.get("dj", {}).get("seed", 0))
    proto = _proto(store, lane, label, items, now)
    proto["recipe"] = recipe
    if vibe := home_vibe.current(store, now):
        proto["recipe"]["vibe"] = vibe
    return proto


def _record_served_cards(store, protos, now):
    """Record only final served tracks, with paired counterfactuals where available."""
    for card_position, proto in enumerate(protos, 1):
        rows = []
        ranker = proto.get("ranker") or "lane"
        comparison = uuid.uuid4().hex
        experiment = proto.get("experiment")
        if experiment:
            experiment = {**experiment, "comparison_id": comparison}
        for track_position, track in enumerate(proto["tracks"], 1):
            key = ordering._field(track, "key")
            details = dict(track) if isinstance(track, dict) else vars(track).copy()
            rows.append({**details, "key": key, "rank": track_position, "lane": proto["lane"],
                         "mode_id": proto.get("mode_id"),
                         "serving_context": {**details.get("serving_context", {}), "ranker": ranker,
                                             "card_position": card_position, "track_position": track_position,
                                             "recipe": proto.get("recipe", {})}})
        share = experiment["share"] if experiment else None
        propensity = (share if ranker == "ppr" else 1 - share) if share is not None else 1.0
        measurement.record_recommendations(store, "home", rows, now, ranker=ranker,
            request_id=comparison + ":live", experiment=experiment, propensity=propensity)
        if experiment and (shadow := proto.get("counterfactual")):
            measurement.record_recommendations(store, "home", shadow["tracks"], now,
                lane=proto["lane"], mode_id=proto.get("mode_id"), ranker=shadow["ranker"],
                request_id=comparison + ":counterfactual", experiment=experiment,
                policy_kind="counterfactual", propensity=0.0, provenance={"recipe": proto.get("recipe", {})})


def _one_card(store, card, now, *, avoid_genres=(), include_genres=()):
    if card == "wheelhouse":
        pool = list(surfaces.for_you(store, now, limit=ROTATION_POOL))
    elif card == "explore":
        fy = {i.key for i in surfaces.for_you(store, now, limit=ROTATION_POOL)}
        pool = [i for i in surfaces.explore_for_you(store, now, limit=ROTATION_POOL) if i.key not in fy]
    elif card == "comfort":
        pool = list(surfaces.comfort_listening(store, now, limit=ROTATION_POOL))
    elif card == "fresh":
        from yt_playlist.rec import fresh_pool
        pool = fresh_pool.pick(store, now, include_genres, avoid_genres, size=ROTATION_POOL)
    else:
        return None
    if avoid_genres or include_genres:
        pool = home_themes.matching_genres(store, pool, avoid=avoid_genres, include=include_genres)
    if not pool or ((avoid_genres or include_genres) and len(pool) < 4):
        return None
    return _carded(store, card, _CARD_LABELS[card], pool, now)


class HomeCards:
    def __init__(self, store: HomeRepositories, jobs: PoolJobs, logger: Logger):
        self.store, self.jobs, self.logger = store, jobs, logger

    def audit(self, protos: list[dict], now: float, source: str) -> None:
        try:
            home_audit.record(self.store, protos, now, source=source, epoch=_epoch(self.store, "cards"),
                              card_epochs={p["lane"]: _epoch(self.store, p["lane"]) for p in protos})
        except Exception:
            self.logger.warning("Home offer audit failed", exc_info=True)

    def offered(self, protos: list[dict], now: float) -> None:
        for proto in protos:
            keys = [ordering._field(t, "key") for t in proto["tracks"]]
            self.store.discovery.mark_offered("track", [key for key in keys if key], now)

    def cards(self, request: CardsRequest) -> CardsResult:
        store, now = self.store, request.now
        from yt_playlist.rec import mode_surfaces
        mix_tuning.expire(store, now)
        epoch = _epoch(store, "cards")
        genres = store.rec_surface.get_proposals("home_genre_request") or {}
        active = request.use_genre_request and genres.get("epoch") == epoch
        avoid = set(genres.get("avoid_genres", [])) if active else set()
        include = set(genres.get("include_genres", [])) if active else set()
        filters = {}
        if avoid:
            filters["avoid_genres"] = avoid
        if include:
            filters["include_genres"] = include
        cards = mode_surfaces.assemble_cards(store, now, epoch, **filters)
        if include:
            cards = [c for c in cards if c["lane"] != "fresh"]
        if cards:
            protos = []
            for card in cards:
                rolled = recipes.roll_recipe(store, card["lane"], seed=f"{epoch}:{card['mode_id']}", now=now)
                journey, dj = rolled.get("journey", "shuffle"), rolled.get("dj", {})
                tracks = _order_by_journey(store, card["tracks"], journey, dj.get("seed", 0))
                shadow = card.get("counterfactual")
                if shadow:
                    shadow = {**shadow, "tracks": _order_by_journey(store, shadow["tracks"], journey, dj.get("seed", 0))}
                protos.append(_proto(store, card["lane"], card["label"], tracks, now)
                              | {"mode_id": card["mode_id"], "ranker": card["ranker"],
                                 "experiment": card.get("experiment"), "counterfactual": shadow,
                                 "recipe": {"model": "mode", "lane": card["lane"], "mode_id": card["mode_id"],
                                            "ranker": card["ranker"], "journey": journey, "dj": dj}})
            try:
                store.modes.log_impressions(epoch, [(c["lane"], c["mode_id"], c["ranker"]) for c in cards], now)
            except Exception:
                self.logger.warning("mode-impression log failed", exc_info=True)
        elif filters and store.rec_surface.get_proposals("mode_bundles") and store.modes.list_modes(active_only=True):
            protos = []
        else:
            protos = [p for p in (_one_card(store, name, now, **filters) for name in _ROW_LANES) if p]
        if filters and not protos and not include:
            return self.cards(CardsRequest(now, request.regenerating, False,
                              "No other genre themes are ready yet. Here are your usual mixes."))
        parameters = {"include_genres": sorted(include), "avoid_genres": sorted(avoid)}
        pool_job = self.jobs.latest("fresh_pool", parameters)
        if include:
            protos = [p for p in protos if p["lane"] != "fresh"]
        if not any(p["lane"] == "fresh" for p in protos):
            if fresh := self.built_fresh(parameters, now):
                protos.append(fresh)
        meta = (store.rec_surface.get_proposals("mode_bundles") or {}).get("_meta", {})
        fourth = ("comfort" if meta.get("comfort_pool", 0) >= rec_params.get_param(store, "comfort_min_pool")
                  or not meta.get("year_cuts") else "temporal")
        fourth = next((p["lane"] for p in protos if p["lane"] in ("comfort", "temporal")), fourth)
        slots = []
        for lane in ("wheelhouse", "explore", "fresh", fourth):
            proto = next((p for p in protos if p["lane"] == lane), None)
            scoped = parameters if lane == "fresh" else {**parameters, "lane": lane, "epoch": epoch}
            if proto is None and lane != "fresh":
                excluded = [ordering._field(t, "key") for p in protos for t in p["tracks"]]
                proto = self.built_library(scoped, now, excluded)
                if proto:
                    protos.append(proto)
            job = pool_job if lane == "fresh" else self.jobs.latest("library_pool", scoped)
            slots.append({"proto": proto, "pool": None if proto else self.pool_view(scoped, job)})
        notice = request.genre_notice
        if filters:
            notice = "Your chosen genres, just for this set." if include else "Different genres for this set of mixes."
            recipe_request = ({"kind": "chosen_genres", "include": sorted(include)} if include
                              else {"kind": "other_genres", "avoid": sorted(avoid)})
            for proto in protos:
                proto["recipe"] = {**(proto.get("recipe") or {}), "genre_request": recipe_request}
        vibe = home_vibe.current(store, now)
        for proto in protos:
            if vibe:
                proto["recipe"] = {**(proto.get("recipe") or {}), "vibe": vibe}
        self.offered(protos, now)
        try:
            _record_served_cards(store, protos, now)
        except Exception:
            self.logger.warning("served-impression log failed", exc_info=True)
        return CardsResult(protos, slots, notice, notice if not filters else None,
                           home_themes.genre_options(store), sorted(include), epoch, vibe)

    @staticmethod
    def pool_label(parameters):
        lane = parameters.get("lane", "fresh")
        return (_CARD_LABELS[lane] if lane != "temporal"
                else {0: "Throwback", 1: "Time Flies", 2: "Recent Picks"}[parameters["epoch"] % 3])

    def pool_view(self, parameters, job=None):
        lane = parameters.get("lane", "fresh")
        fresh = lane == "fresh"
        building = job is not None and not job.done
        message = job.events[-1]["text"] if building and job.events else "Finding matching songs…"
        if job and job.done:
            message = job.error or ("Not enough matching songs yet. Try another search or adjust your genres."
                                    if fresh else "Not enough matching songs in your library. Try adjusting your genres.")
        labels = {o["family"]: o["label"] for o in home_themes.genre_options(self.store)}
        label = self.pool_label(parameters)
        return {"parameters": parameters, "job": job, "building": building, "message": message,
                "lane": lane, "label": label,
                "action": "Find fresh songs" if fresh else f"Build {label.lower()}",
                "title": "Discover a fresh mix" if fresh else "Build this mix",
                "building_title": "Building your fresh mix" if fresh else "Building your mix",
                "description": ("Find new songs for these genres. Click to start." if fresh
                                else "Find matching songs in your library. Click to start."),
                "url": "/home/fresh-pool" if fresh else "/home/library-pool",
                "genre_labels": [labels.get(g, g) for g in parameters["include_genres"]]}

    def built_library(self, parameters, now, exclude_keys):
        from yt_playlist.rec import library_pool
        tracks = library_pool.pick(self.store, now, parameters, exclude_keys)
        if len(tracks) < 4:
            return None
        proto = _carded(self.store, parameters["lane"], self.pool_label(parameters), tracks, now)
        if len(proto["tracks"]) < 4:
            return None
        proto["mode_id"] = None
        include, avoid = parameters["include_genres"], parameters["avoid_genres"]
        if include or avoid:
            proto["recipe"]["genre_request"] = ({"kind": "chosen_genres", "include": include} if include
                                                else {"kind": "other_genres", "avoid": avoid})
        return proto

    def built_fresh(self, parameters, now):
        include, avoid = parameters["include_genres"], parameters["avoid_genres"]
        if include:
            from yt_playlist.rec import fresh_pool
            tracks = fresh_pool.pick(self.store, now, include, avoid)
            if not tracks:
                return None
            proto = _proto(self.store, "fresh", _CARD_LABELS["fresh"], tracks, now)
            proto["recipe"] = recipes.roll_recipe(self.store, "fresh", seed=f"fresh:{_epoch(self.store, 'fresh')}", now=now)
            proto["recipe"]["facets"] = {"genres": include}
        else:
            proto = _one_card(self.store, "fresh", now, **({"avoid_genres": avoid} if avoid else {}))
        if proto is not None:
            if len(proto["tracks"]) < 4:
                return None
            if include or avoid:
                proto["recipe"]["genre_request"] = ({"kind": "chosen_genres", "include": include} if include
                                                    else {"kind": "other_genres", "avoid": avoid})
            if vibe := home_vibe.current(self.store, now):
                proto["recipe"]["vibe"] = vibe
        return proto

    def pool(self, job: PoolJob, now: float, source: str) -> PoolResult:
        proto = None
        if job.done and not job.error:
            proto = (self.built_fresh(job.parameters, now) if job.kind == "fresh_pool"
                     else self.built_library(job.parameters, now, job.exclude_keys))
        if proto:
            self.offered([proto], now)
            _record_served_cards(self.store, [proto], now)
            self.audit([proto], now, source)
            return PoolResult(proto=proto)
        return PoolResult(pool=self.pool_view(job.parameters, job))

    def start_pool(self, kind: str, parameters: dict, excluded: list[str]) -> PoolJob:
        """Only explicit build requests admit jobs; page/menu reads never start work."""
        if kind not in ("fresh_pool", "library_pool"):
            raise ValueError("Unknown Home pool kind")
        return self.jobs.start(kind, parameters, excluded)

    def refresh_card(self, card: str, now: float) -> dict | None:
        self.store.rec_surface.refresh_card(card, max(1, rec_params.get_param(self.store, "erosion_view_cap")), now)
        proto = _one_card(self.store, card, now)
        if proto:
            self.offered([proto], now)
        return proto
