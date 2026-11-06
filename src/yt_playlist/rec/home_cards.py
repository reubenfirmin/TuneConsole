"""Home recommendation application service, independent of HTTP and templates.

The existing recommendation engines own ranking. This module assembles their final
offers and records mode impressions and diagnostic audits. The web adapter captures
visible menu choices; building a mix does not expose its individual tracks.
Job admission stays behind the small PoolJobs port; the web adapter owns execution.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from logging import Logger
from typing import Any, Protocol
import hashlib
import json

from yt_playlist.rec import (arc_energy, home_audit, home_themes, home_vibe, journeys, musical_character,
                             mix_tuning, ordering, rec_params, recipes, surfaces)
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.rec_ledger import RecLedgerRepo
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.rec.mode_selection import PROTO_SIZE, ROTATION_POOL, CARD_LABELS as _CARD_LABELS
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key

_ROW_LANES = ("wheelhouse", "explore", "comfort", "fresh")


class HomeRepositories(Protocol):
    discovery: DiscoveryRepo
    modes: ModesRepo
    rec_model: RecModelRepo
    rec_query: RecQueryRepo
    rec_ledger: RecLedgerRepo
    rec_proposals: RecProposalsRepo
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
    epoch: int | None = None
    mode_order: tuple | None = None
    record: bool = True


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
    choice_context: dict | None = None
    preparing: bool = False
    preference_notice: str | None = None


@dataclass
class PoolResult:
    proto: dict | None = None
    pool: dict | None = None


def experiment_id(proto):
    """Account of the choice, independent of its current track order or cache generation."""
    choice = {"lane": proto["lane"], "label": proto.get("label"), "note": proto.get("note"),
              "genres": sorted(set(proto.get("genres") or []))}
    return hashlib.sha256(json.dumps(choice, sort_keys=True).encode()).hexdigest()[:24]


def _proto(store, lane, label, items, now, *, vibe=None, catalog=None):
    when = datetime.fromtimestamp(now).strftime("%B %-d %Y")
    tracks = items[:PROTO_SIZE]
    counts = recipes.theme_counts(store, tracks, catalog=catalog)
    genres, _ = counts
    purpose = home_vibe.PRESETS.get(vibe, {}).get('label')
    name = f"{label} - {purpose} - {when}" if purpose else f"{label} - {when}"
    return {"lane": lane, "label": label, "name": name,
            "note": recipes.theme_sentence(store, lane, tracks, counts=counts), "tracks": tracks,
            "genres": list(genres), "feedback_surface": "for_you" if lane == "fresh" else None}


def _order_by_journey(store, items, journey, seed, selection_data=None):
    catalog = (selection_data or {}).get('journey_catalog')
    items = ordering.attach_genres(store, items, genres=catalog['genres'] if catalog is not None else None)
    field = ordering._field
    keys = [field(it, "key") or "" for it in items]
    dao = store.rec_query
    decades = ((catalog['decades'] if catalog is not None else dao.track_decades(keys))
               if journey in ('time_machine', 'throwback') else {})
    lastp = {}
    if journey == 'rediscovery':
        history_keys = tuple(sorted(set(catalog.get('keys', keys) if catalog is not None else keys)))
        lastp = store.memoized_read(('journey-last-played', history_keys),
                                   lambda: dao.track_last_played(history_keys))
    plays = ((selection_data['plays'] if selection_data is not None and 'plays' in selection_data else
              store.memoized_read('journey-play-counts', dao.play_counts))
             if journey == 'deep_dive' else {})
    axes = (catalog['axes'] if catalog is not None else
            store.genres.feature_axes([field(it, 'genre') or '' for it in items]))
    profiles = musical_character.load(store, [field(it, 'key') for it in items
        if not (isinstance(it, dict) and 'musical' in it)])
    # A prepared selection owns its profile until its next worker publication.
    profiles.update({field(it, 'key'): it['musical'] for it in items
                     if isinstance(it, dict) and 'musical' in it})
    arc = arc_energy.arc_energies(keys, profiles)

    def feat(it):
        key, genre = field(it, "key") or "", field(it, "genre") or ""
        return {"artist": field(it, "artist") or "", "genre": genre,
                "family": axes.get(genre, ('', None))[0],
                "energy": arc.get(key, .5), "decade": decades.get(key),
                "plays": plays.get(key, 0), "recency": lastp.get(key, 0.0)}

    ordered = journeys.journey_order(items, journey, seed, feat)
    # Keep the exact cached inputs used by the journey with served tracks.
    # Outcomes then resolve to these inputs after later preparation changes.
    return [{**item, 'musical': profiles.get(field(item, 'key')),
             'serving_context': {**(item.get('serving_context') or {}),
                 **musical_character.provenance({'musical': profiles.get(field(item, 'key'))})}}
            if isinstance(item, dict) else item for item in ordered]


def _group_cards(store, now, epoch, prepared, include, avoid):
    from yt_playlist.rec import group_home
    groups = group_home.inputs(store, now, prepared)
    if not groups:
        return None
    return {'cards': group_home.assemble(store, now, epoch, groups, include=include, avoid=avoid),
            'context': groups['context']}


def _completion_catalog(store, cards):
    """Resolve the displayed tracks and their shadows once per pinned input set."""
    items = [item for card in cards
             for option in (card, card.get('counterfactual') or {})
             for item in option.get('tracks', [])]
    fields = [(ordering._field(item, 'key') or identity_key(ordering._field(item, 'title') or '',
               ordering._field(item, 'artist') or ''), ordering._field(item, 'genre') or '',
               ordering._field(item, 'artist') or '') for item in items]
    def read():
        genres = store.rec_query.track_genres(list(dict.fromkeys(key for key, _, _ in fields)))
        resolved = [genres.get(key) or fallback for key, fallback, _ in fields]
        artists = list(dict.fromkeys(artist for (key, fallback, artist) in fields
                                    if not (genres.get(key) or fallback)))
        return {'keys': tuple(dict.fromkeys(key for key, _, _ in fields)),
                'genres': genres, 'axes': store.genres.feature_axes(resolved),
                'decades': store.rec_query.track_decades(list(dict.fromkeys(
                    ordering._field(item, 'key') or '' for item in items))),
                'artists': store.rec_query.artist_genre_years_for(artists) if artists else {}}
    # Calling the memo inside each card's completion also propagates its SQL
    # dependencies into every independently cached completed card.
    return store.memoized_read(('home-completion-catalog', tuple(sorted(set(fields)))), read)


def _complete_card(store, card, now, epoch, prepared, selection_data):
    vibe = home_vibe.current(store, now)
    rolled = recipes.roll_recipe(store, card['lane'], seed=f"{epoch}:{card['mode_id']}", now=now,
        ordering_only=True,
        **({'leans': prepared['leans'], 'dists': prepared['dists']} if prepared is not None else {}))
    journey, dj = rolled.get('journey', 'shuffle'), rolled.get('dj', {})
    tracks = _order_by_journey(store, card['tracks'], journey, dj.get('seed', 0), selection_data)
    shadow = card.get('counterfactual')
    if shadow:
        shadow = {**shadow, 'tracks': _order_by_journey(store, shadow['tracks'], journey,
                                                      dj.get('seed', 0), selection_data)}
    proto = _proto(store, card['lane'], card['label'], tracks, now, vibe=vibe,
                   catalog=(selection_data or {}).get('journey_catalog')) | {
        'mode_id': card['mode_id'], 'ranker': card['ranker'], 'experiment': card.get('experiment'),
        'counterfactual': shadow,
        'recipe': {'model': 'vibe' if vibe else 'mode', 'lane': card['lane'],
                   'mode_id': card['mode_id'], 'ranker': card['ranker'], 'journey': journey, 'dj': dj,
                   'musical_version': musical_character.VERSION}}
    if card.get('group_version'):
        identity = {key: card[key] for key in ('group_id', 'group_version', 'group_generation')}
        proto.update(identity)
        proto['recipe'].update(identity, model='musical-group')
    return proto


def _epoch(store, card):
    if card == 'cards':
        return mix_tuning.card_epoch(store)
    cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
    return max(0, store.rec_ledger.card_views(card) - 1) // cap


def _rolled_theme(store, lane, pool, now, preparation=None):
    dists = recipes.pool_facets(store, pool)
    avoid = {"genres": set(), "eras": set()}
    for other in _ROW_LANES:
        this_one = other == lane
        recipe = recipes.roll_recipe(store, other, seed=f"{other}:{_epoch(store, other)}",
                                     now=now, avoid=avoid,
                                     dists=dists if this_one else (preparation or {}).get('dists'),
                                     **({'leans': preparation['leans']} if preparation is not None else {}))
        if this_one:
            return recipe
        avoid["genres"] |= set(recipe.get("facets", {}).get("genres", []))
        avoid["eras"] |= set(recipe.get("facets", {}).get("eras", []))
    return recipes.roll_recipe(store, lane, seed=f"{lane}:{_epoch(store, lane)}", now=now, dists=dists)


def _carded(store, lane, label, pool, now, preparation=None):
    vibe = home_vibe.current(store, now)
    pool = ordering.attach_genres(store, list(pool))
    recipe = _rolled_theme(store, lane, pool, now, preparation)
    focused = pool if vibe else recipes.theme_filter(store, pool, recipe.get("facets", {}))
    focused = home_vibe.rank(store, now, focused)
    items = (focused[:PROTO_SIZE] if vibe else
             surfaces.rotate_sample(focused[:PROTO_SIZE * 2], PROTO_SIZE, _epoch(store, lane)))
    items = _order_by_journey(store, items, recipe.get("journey", "shuffle"), recipe.get("dj", {}).get("seed", 0))
    proto = _proto(store, lane, label, items, now, vibe=vibe)
    proto["recipe"] = recipe
    if vibe:
        proto["recipe"]["vibe"] = vibe
    return proto


def _one_card(store, card, now, *, avoid_genres=(), include_genres=(), exclude_keys=(), preparation=None):
    if card == "wheelhouse":
        pool = list(surfaces.for_you(store, now, limit=ROTATION_POOL))
    elif card == "explore":
        pool = list(surfaces.explore_for_you(store, now, limit=ROTATION_POOL + len(exclude_keys)))
    elif card == "comfort":
        pool = list(surfaces.comfort_listening(store, now, limit=ROTATION_POOL))
    elif card == "fresh":
        from yt_playlist.rec import fresh_pool
        pool = fresh_pool.pick(store, now, include_genres, avoid_genres, size=ROTATION_POOL,
                               bundles=(preparation or {}).get('bundles'))
    else:
        return None
    if exclude_keys:
        excluded = set(exclude_keys)
        pool = [i for i in pool if ordering._field(i, "key") not in excluded]
    if avoid_genres or include_genres:
        pool = home_themes.matching_genres(store, pool, avoid=avoid_genres, include=include_genres)
    if not pool or ((avoid_genres or include_genres) and len(pool) < 4):
        return None
    return _carded(store, card, _CARD_LABELS[card], pool, now, preparation)


class HomeCards:
    def __init__(self, store: HomeRepositories, jobs: PoolJobs, logger: Logger, *, genre_options=None,
                 selection_data=None):
        self.store, self.jobs, self.logger = store, jobs, logger
        self.genre_options, self.selection_data = genre_options, selection_data

    def options(self):
        return self.genre_options if self.genre_options is not None else home_themes.genre_options(self.store)

    def audit(self, protos: list[dict], now: float, source: str, *, reader=None) -> None:
        if getattr(self.store, 'activity_paused', False) or getattr(self.store, '_read_only', False):
            return
        reader = self.store if reader is None else reader
        try:
            home_audit.record(reader, protos, now, source=source, epoch=_epoch(reader, "cards"),
                              card_epochs={p["lane"]: _epoch(reader, p["lane"]) for p in protos})
        except Exception:
            self.logger.warning("Home offer audit failed", exc_info=True)

    def cards(self, request: CardsRequest) -> CardsResult:
        store, now = self.store, request.now
        from yt_playlist.rec import mode_surfaces
        mix_tuning.expire(store, now)
        epoch = _epoch(store, "cards") if request.epoch is None else request.epoch
        genres = store.rec_proposals.get_proposals("home_genre_request") or {}
        vibe = home_vibe.current(store, now)
        saved = request.use_genre_request and mix_tuning.genre_active(store, genres)
        active = saved
        avoid = set(genres.get("avoid_genres", [])) if active else set()
        include = set(genres.get("include_genres", [])) if active else set()
        filters = {}
        if avoid:
            filters["avoid_genres"] = avoid
        if include:
            filters["include_genres"] = include
        prepared = (self.selection_data or {}).get('preparation')
        checkpoint = (prepared or {}).get('checkpoint')
        group_key = ('home-group-cards', checkpoint, epoch, tuple(sorted(include)), tuple(sorted(avoid)))
        groups = (store.memoized_read(group_key, lambda: _group_cards(store, now, epoch, prepared, include, avoid))
                  if checkpoint is not None else _group_cards(store, now, epoch, prepared, include, avoid))
        cards = (groups['cards'] if groups else
                 mode_surfaces.assemble_cards(store, now, epoch, **filters,
                                             **({"mode_order": request.mode_order} if request.mode_order is not None else {}),
                                             **({'preparation': prepared} if prepared is not None else {})))
        assembly = (prepared or {}).get('assembly')
        typed = getattr(store, '_recommendation_files', False)
        bundles = (None if typed else prepared['bundles'] if prepared is not None else
                   assembly['bundles'] if assembly is not None else store.rec_proposals.get_proposals('mode_bundles'))
        meta = (mode_surfaces.bundle_metadata(store) if typed else (bundles or {}).get('_meta', {}))
        if include:
            cards = [c for c in cards if c["lane"] != "fresh"]
        if cards:
            protos = []
            completion = prepared
            if completion is None and not vibe:
                from yt_playlist.rec import transient
                from yt_playlist.rec.taste_analysis import era_distribution, taste_breadth
                completion = {'leans': transient.facet_leans(store, now),
                              'dists': (taste_breadth(store)['families'], dict(era_distribution(store)))}
            for card in cards:
                key = ('home-complete-card', checkpoint, epoch, card['lane'], card.get('group_version'),
                       card.get('mode_id'), tuple(t['key'] for t in card['tracks']))
                complete = lambda: _complete_card(store, card, now, epoch, completion,
                    {**(self.selection_data or {}), 'journey_catalog': _completion_catalog(store, cards)})
                protos.append(store.memoized_read(key, complete) if checkpoint is not None else complete())
            if request.record:
                self.record_modes(protos, epoch, now)
        elif typed:
            from yt_playlist.rec import stored_home
            protos = ([] if vibe else stored_home.unmodeled_cards(store, now, include=include, avoid=avoid,
                                                                 preparation=prepared, card_builder=_carded))
        elif not vibe and assembly is not None and not assembly['modes']:
            # Cold imports have prepared library pools before modes can be fitted.
            # This only assembles cached candidates; ranking stays in the worker.
            protos = []
            inputs = assembly
            for lane in _ROW_LANES:
                if lane == 'fresh':
                    continue  # built_fresh applies discovery ownership and eligibility below
                pool = [t for t in bundles.get('all', {}).get(lane, [])
                        if t['key'] not in inputs['blocked'] and t['artist'] not in inputs['muted']]
                pool = home_themes.matching_genres(store, pool, include=include, avoid=avoid)
                if lane == 'explore':
                    used = {t['key'] for p in protos for t in p['tracks']}
                    pool = [t for t in pool if t['key'] not in used]
                if len(pool) >= 4:
                    proto = _carded(store, lane, _CARD_LABELS[lane], pool[:ROTATION_POOL], now)
                    if len(proto['tracks']) >= 4:
                        protos.append(proto)
        elif (groups or vibe or prepared is not None or (filters and bundles and store.modes.list_modes(active_only=True))):
            protos = []
        else:
            protos = []
            for name in _ROW_LANES:
                options = dict(filters)
                if name == "explore":
                    options["exclude_keys"] = [ordering._field(t, "key") for p in protos for t in p["tracks"]]
                if proto := _one_card(store, name, now, **options):
                    protos.append(proto)
        if filters and not protos and not include and not vibe:
            return self.cards(replace(request, use_genre_request=False,
                              genre_notice="No other genre themes are ready yet. Here are your usual mixes."))
        parameters = {"include_genres": sorted(include), "avoid_genres": sorted(avoid)}
        pool_job = self.jobs.latest("fresh_pool", parameters)
        if include:
            protos = [p for p in protos if p["lane"] != "fresh"]
        if not any(p["lane"] == "fresh" for p in protos):
            if fresh := self.built_fresh(parameters, now):
                protos.append(fresh)
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
        for proto in protos:
            proto["recipe"] = {**(proto.get("recipe") or {}),
                               "musical_version": musical_character.VERSION,
                               "vibe_policy": home_vibe.VERSION, "vibe": vibe}
            proto["experiment_id"] = experiment_id(proto)
            proto["recipe"] = {**(proto.get("recipe") or {}), "experiment_id": proto["experiment_id"]}
        return CardsResult(protos, slots, notice, notice if not filters else None,
                           self.options(), sorted(genres.get('include_genres', [])) if saved else [], epoch, vibe,
                           choice_context=groups['context'] if groups else None)

    def record_modes(self, protos, epoch, now):
        try:
            store = self.store
            store.modes.log_impressions(epoch, [(p["lane"], p["mode_id"], p.get("ranker"))
                                                for p in protos if p.get("mode_id") is not None], now)
        except Exception:
            self.logger.warning("mode-impression log failed", exc_info=True)

    @staticmethod
    def pool_label(parameters):
        lane = parameters.get("lane", "fresh")
        return (_CARD_LABELS[lane] if lane != "temporal"
                else {0: "Throwback", 1: "Time Flies", 2: "Recent Picks"}[parameters["epoch"] % 3])

    def pool_view(self, parameters, job=None, *, genre_options=None, done=None):
        lane = parameters.get("lane", "fresh")
        fresh = lane == "fresh"
        if done is None:
            # A prepared Home menu can predate this job's output. Successful
            # completion alone cannot establish an empty pool: retain its poll
            # until pool() checks current output, including after a board refresh.
            done = job is not None and job.done and bool(job.error)
        building = job is not None and not done
        message = job.events[-1]["text"] if building and job.events else "Finding matching songs…"
        if job and done:
            if job.error:
                from yt_playlist.core.action_errors import MESSAGES
                code = getattr(job, 'validation_code', None)
                allowed = {'fresh_account_required', 'fresh_genres_required', 'fresh_preferences_changed'}
                message = (MESSAGES[code] if fresh and code in allowed else
                           ('Fresh mix not completed. You can try again.' if fresh
                            else 'Mix not completed. You can try again.'))
            else:
                message = ("Not enough matching songs yet. Try another search or adjust your genres."
                           if fresh else "Not enough matching songs in your library. Try adjusting your genres.")
        labels = {o["family"]: o["label"] for o in (self.options() if genre_options is None else genre_options)}
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
        preparation = (self.selection_data or {}).get('preparation')
        if vibe := home_vibe.current(self.store, now):
            from yt_playlist.rec import vibe_pools
            tracks = vibe_pools.pick(self.store, now, vibe, 'fresh',
                                     include_genres=include, avoid_genres=avoid)
            if len(tracks) < 4:
                return None
            proto = _carded(self.store, 'fresh', _CARD_LABELS['fresh'], tracks, now,
                            preparation=preparation)
            if include or avoid:
                proto['recipe']['genre_request'] = ({'kind': 'chosen_genres', 'include': include} if include
                                                    else {'kind': 'other_genres', 'avoid': avoid})
            return proto
        if include:
            from yt_playlist.rec import fresh_pool
            tracks = fresh_pool.pick(self.store, now, include, avoid,
                                     bundles=(preparation or {}).get('bundles'))
            if not tracks:
                return None
            proto = _proto(self.store, "fresh", _CARD_LABELS["fresh"], tracks, now)
            proto["recipe"] = recipes.roll_recipe(self.store, "fresh", seed=f"fresh:{_epoch(self.store, 'fresh')}", now=now,
                **({'leans': preparation['leans'], 'dists': preparation['dists']} if preparation is not None else {}))
            proto["recipe"]["facets"] = {"genres": include}
        else:
            proto = _one_card(self.store, "fresh", now, preparation=preparation,
                              **({"avoid_genres": avoid} if avoid else {}))
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
        # Keep the output check and polling state on the same completion
        # observation. A job finishing between them still needs another poll.
        done = job.done
        if done and not job.error:
            proto = (self.built_fresh(job.parameters, now) if job.kind == "fresh_pool"
                     else self.built_library(job.parameters, now, job.exclude_keys))
        if proto:
            proto["experiment_id"] = experiment_id(proto)
            proto["recipe"]["experiment_id"] = proto["experiment_id"]
            self.audit([proto], now, source)
            return PoolResult(proto=proto)
        return PoolResult(pool=self.pool_view(job.parameters, job, done=done))

    def start_pool(self, kind: str, parameters: dict, excluded: list[str]) -> PoolJob:
        """Only explicit build requests admit jobs; page/menu reads never start work."""
        if kind not in ("fresh_pool", "library_pool"):
            raise ValueError("Unknown Home pool kind")
        return self.jobs.start(kind, parameters, excluded)

    def refresh_card(self, card: str, now: float) -> dict | None:
        self.store.rec_ledger.refresh_card(card, max(1, rec_params.get_param(self.store, "erosion_view_cap")), now)
        return _one_card(self.store, card, now)
