"""Home recommendation application service, independent of HTTP and templates.

The existing recommendation engines own ranking. This module assembles their final
offers and records mode impressions and diagnostic audits. The web adapter captures
visible menu choices; building a mix does not expose its individual tracks.
Job admission stays behind the small PoolJobs port; the web adapter owns execution.
"""
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from logging import Logger
from typing import Any, Protocol
from copy import deepcopy

from yt_playlist.rec import (arc_energy, home_audit, home_themes, home_vibe, journeys, musical_character,
                             mix_tuning, ordering, rec_params, recipes, surfaces)
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.controllers.rec_model import RecommendationModel
from yt_playlist.controllers.recommendation_query import RecommendationQuery
from yt_playlist.controllers.recommendation_ledger import RecommendationLedger
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.recommendation_publications import MixCandidates
from yt_playlist.controllers.settings import Settings
from yt_playlist.rec.mode_selection import PROTO_SIZE, ROTATION_POOL, CARD_LABELS as _CARD_LABELS
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key
from yt_playlist.core.version_policy import VersionPolicy

_ROW_LANES = ("wheelhouse", "explore", "comfort", "fresh")


class HomeRepositories(Protocol):
    discovery: Discovery
    modes: ModesRepo
    rec_model: RecommendationModel
    rec_query: RecommendationQuery
    rec_ledger: RecommendationLedger
    rec_proposals: RecommendationProposals
    settings: Settings


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
    version_policy: VersionPolicy | None = None


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


@dataclass(frozen=True)
class MixBatch:
    groups: dict | None
    home_generation: int | None
    candidates: MixCandidates


from yt_playlist.rec.home_card_values import experiment_id


def _proto(store, lane, label, items, now, *, vibe=None, catalog=None):
    tracks = items[:PROTO_SIZE]
    counts = recipes.theme_counts(store, tracks, catalog=catalog)
    genres, _ = counts
    note = recipes.theme_sentence(store, lane, tracks, counts=counts)
    return project_proto(lane, label, tracks, now, genres, note, vibe=vibe)


def project_proto(lane, label, tracks, now, genres, note, *, vibe=None):
    """Present a starter from supplied tracks and acquired theme facts."""
    when = datetime.fromtimestamp(now).strftime("%B %-d %Y")
    purpose = home_vibe.PRESETS.get(vibe, {}).get('label')
    name = f"{label} - {purpose} - {when}" if purpose else f"{label} - {when}"
    return {"lane": lane, "label": label, "name": name,
            "note": note, "tracks": tracks,
            "genres": list(genres), "feedback_surface": "for_you" if lane == "fresh" else None}


def _order_by_journey(store, items, journey, seed, selection_data=None):
    catalog = (selection_data or {}).get('journey_catalog')
    genres = (catalog['genres'] if catalog is not None
              else store.rec_query.track_genres(ordering.genre_keys(items)))
    items = ordering.attach_genres(items, genres=genres)
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
    plays = {}
    if journey == 'deep_dive':
        history_keys = tuple(sorted(set(catalog.get('keys', keys) if catalog is not None else keys)))
        plays = store.memoized_read(('journey-play-counts', history_keys),
                                    lambda: store.recommendations.track_play_counts(history_keys))
    axes = (catalog['axes'] if catalog is not None else
            store.genres.feature_axes([field(it, 'genre') or '' for it in items]))
    profile_keys = [field(it, 'key') for it in items if not (isinstance(it, dict) and 'musical' in it)]
    saved_profiles = (catalog or {}).get('profiles')
    profiles = (musical_character.load(store, profile_keys) if saved_profiles is None
                else {key: saved_profiles.get(key) for key in profile_keys})
    # A prepared selection owns its profile until its next worker publication.
    profiles.update({field(it, 'key'): it['musical'] for it in items
                     if isinstance(it, dict) and 'musical' in it})
    missing_credits = [key for key, item in zip(keys, items, strict=True)
                       if field(item, 'artist_ids') is None]
    identities = store.artists.track_keys(keys=missing_credits) if missing_credits else {}
    return journey_values(items, journey, seed, decades, lastp, plays, axes, profiles, identities=identities)


def journey_values(items, journey, seed, decades, lastp, plays, axes, profiles, *, identities=None):
    """Order a card with captured listening, musical and taxonomy facts."""
    field = ordering._field
    keys = [field(item, 'key') or '' for item in items]
    arc = arc_energy.arc_energies(keys, profiles)

    def feat(it):
        key, genre = field(it, "key") or "", field(it, "genre") or ""
        return {"artist": field(it, "artist") or "", "genre": genre,
                'artist_ids': (identities or {}).get(key) or field(it, 'artist_ids'),
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


def _group_cards(store, now, epoch, groups, include, avoid, batch=None):
    from yt_playlist.rec import group_home
    if not groups:
        return None
    return {'cards': group_home.assemble(store, now, epoch, groups, include=include, avoid=avoid,
                                         mix_candidates=batch.candidates if batch is not None else None),
            'context': groups['context']}


def _completion_catalog(store, cards, *, known_metadata=False):
    """Resolve the displayed tracks and their shadows once per pinned input set."""
    items = [item for card in cards
             for option in (card, card.get('counterfactual') or {})
             for item in option.get('tracks', [])]
    fields = [(ordering._field(item, 'key') or identity_key(ordering._field(item, 'title') or '',
               ordering._field(item, 'artist') or ''), ordering._field(item, 'genre') or '',
               ordering._field(item, 'artist') or '') for item in items]
    profile_keys = tuple(sorted({ordering._field(item, 'key') for item in items
                                if isinstance(item, dict) and 'musical' not in item and item.get('key')}))
    if known_metadata:
        keys = tuple(dict.fromkeys(key for key, _, _ in fields))
        decades = {item['key']: str(int(item['year']) // 10 * 10) for item in items if item.get('year')}
        return {'keys': keys, 'genres': {}, 'axes': store.genres.feature_axes({genre for _, genre, _ in fields}),
                'decades': decades, 'profiles': musical_character.load(store, profile_keys), 'artists': {}}

    def read():
        keys = tuple(dict.fromkeys(key for key, _, _ in fields))
        genres, decades = store.rec_query.track_genres_and_decades(keys)
        resolved = [genres.get(key) or fallback for key, fallback, _ in fields]
        artists = list(dict.fromkeys(artist for (key, fallback, artist) in fields
                                    if not (genres.get(key) or fallback)))
        return {'keys': keys, 'genres': genres, 'axes': store.genres.feature_axes(resolved),
                'decades': decades, 'profiles': musical_character.load(store, profile_keys),
                'artists': store.rec_query.artist_genre_years_for(artists) if artists else {}}
    return store.memoized_read(('home-completion-catalog', tuple(sorted(set(fields))), profile_keys), read)


def _complete_card(store, card, now, epoch, prepared, selection_data):
    vibe = home_vibe.current(store, now)
    style = card.get('group_version') or card['mode_id']
    rolled = recipes.roll_recipe(store, card['lane'], seed=f"{epoch}:{style}", now=now,
        ordering_only=True,
        leans={}, dists=({}, {}) if vibe else None if prepared is None else prepared['dists'])
    journey, dj = rolled.get('journey', 'shuffle'), rolled.get('dj', {})
    tracks = _order_by_journey(store, card['tracks'], journey, dj.get('seed', 0), selection_data)
    shadow = card.get('counterfactual')
    if shadow:
        shadow = {**shadow, 'tracks': _order_by_journey(store, shadow['tracks'], journey,
                                                      dj.get('seed', 0), selection_data)}
    proto = _proto(store, card['lane'], card['label'], tracks, now, vibe=vibe,
                   catalog=(selection_data or {}).get('journey_catalog'))
    return complete_card_values(card, proto, shadow, vibe, journey, dj)


def complete_card_values(card, proto, shadow, vibe, journey, dj):
    proto = proto | {
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


def _rolled_theme(store, lane, pool, now, preparation=None, *, theme_avoid=None):
    dists = recipes.pool_facets(store, pool)
    if theme_avoid is not None:
        return recipes.roll_recipe(store, lane, seed=f"{lane}:{_epoch(store, lane)}", now=now,
                                   avoid=theme_avoid, dists=dists, leans={})
    avoid = {"genres": set(), "eras": set()}
    for other in _ROW_LANES:
        this_one = other == lane
        recipe = recipes.roll_recipe(store, other, seed=f"{other}:{_epoch(store, other)}",
                                     now=now, avoid=avoid,
                                     dists=dists if this_one else (preparation or {}).get('dists'),
                                     leans={})
        if this_one:
            return recipe
        avoid["genres"] |= set(recipe.get("facets", {}).get("genres", []))
        avoid["eras"] |= set(recipe.get("facets", {}).get("eras", []))
    return recipes.roll_recipe(store, lane, seed=f"{lane}:{_epoch(store, lane)}", now=now, dists=dists, leans={})


def _carded(store, lane, label, pool, now, preparation=None, *, theme_avoid=None, vibe_selected=False):
    vibe = home_vibe.current(store, now)
    pool = list(pool)
    if vibe:
        # The preset controls selection, so only roll settings for sequencing.
        recipe = recipes.roll_recipe(store, lane, seed=f"{lane}:{_epoch(store, lane)}", now=now,
                                     dists=({}, {}), leans={})
    else:
        ordering.attach_genres(pool, genres=store.rec_query.track_genres(ordering.genre_keys(pool)))
        recipe = _rolled_theme(store, lane, pool, now, preparation, theme_avoid=theme_avoid)
    focused = pool if vibe else recipes.theme_filter(store, pool, recipe.get("facets", {}))
    if not vibe_selected:
        focused = home_vibe.rank(store, now, focused)
    items = (focused[:PROTO_SIZE] if vibe else
             surfaces.rotate_sample(focused[:PROTO_SIZE * 2], PROTO_SIZE, _epoch(store, lane)))
    catalog = _completion_catalog(store, [{'tracks': items}], known_metadata=True) if vibe_selected else None
    items = _order_by_journey(store, items, recipe.get("journey", "shuffle"), recipe.get("dj", {}).get("seed", 0),
                             {'journey_catalog': catalog})
    proto = _proto(store, lane, label, items, now, vibe=vibe, catalog=catalog)
    proto["recipe"] = recipe
    if vibe:
        proto["recipe"]["vibe"] = vibe
    return proto


def _one_card(store, card, now, *, avoid_genres=(), include_genres=(), exclude_keys=(), preparation=None,
              mix_candidates=None, card_builder=None):
    from yt_playlist.library import mix_listening

    if card == "wheelhouse":
        pool = list(surfaces.for_you(store, now, limit=ROTATION_POOL, mix_listening=mix_listening.read(store, now)))
    elif card == "explore":
        pool = list(surfaces.explore_for_you(store, now, limit=ROTATION_POOL + len(exclude_keys),
                                           mix_listening=mix_listening.read(store, now)))
    elif card == "comfort":
        pool = list(surfaces.comfort_listening(store, now, limit=ROTATION_POOL))
    elif card == "fresh":
        from yt_playlist.rec import fresh_pool
        pool = fresh_pool.pick(store, now, include_genres, avoid_genres, size=ROTATION_POOL,
                               bundles=(preparation or {}).get('bundles'), mix_candidates=mix_candidates)
    else:
        return None
    if exclude_keys:
        excluded = set(exclude_keys)
        pool = [i for i in pool if ordering._field(i, "key") not in excluded]
    if avoid_genres or include_genres:
        pool = home_themes.matching_genres(store, pool, avoid=avoid_genres, include=include_genres)
    if not pool or ((avoid_genres or include_genres) and len(pool) < 4):
        return None
    return (card_builder or _carded)(store, card, _CARD_LABELS[card], pool, now, preparation)


class HomeCards:
    def __init__(self, store: HomeRepositories, jobs: PoolJobs, logger: Logger, *, genre_options=None,
                 selection_data=None, reuse_mix_candidates=False):
        self.store, self.jobs, self.logger = store, jobs, logger
        self.genre_options, self.selection_data = genre_options, selection_data
        self.reuse_mix_candidates = reuse_mix_candidates
        self._mix_batch = None
        self._mix_batch_version = None
        self._mix_batch_dependencies = ()

    @classmethod
    def live(cls, store, jobs, logger, summary):
        distributions = (summary['breadth']['families'], dict(summary['eras']))
        return cls(store, jobs, logger, genre_options=summary['genre_options'],
                   selection_data={'library_distributions': distributions}, reuse_mix_candidates=True)

    def _load_mix_batch(self, now, prepared):
        from yt_playlist.rec import fresh_pool, group_home, group_viz, stored_home
        store = self.store
        groups = group_home.inputs(store, now, prepared)
        head = store.recommendations.head('home')
        home_generation = head['id'] if head is not None else None
        slots = []
        if groups:
            generation = groups['recommendation_generation']
            slots.extend(('home', generation, 'all', lane)
                         for lane in group_home.library_lanes(store, groups['meta']))
            groups['memberships'] = group_viz.recording_groups(store, groups['preview']['generation'])
        if home_generation is not None:
            slots.extend(stored_home.unmodeled_slots(home_generation))
        candidates = store.recommendations.mix_candidates(list(dict.fromkeys(slots)), fresh_pool.SLOTS)
        return MixBatch(groups, home_generation, candidates)

    def options(self):
        return self.genre_options if self.genre_options is not None else home_themes.genre_options(self.store)

    def audit(self, protos: list[dict], now: float, source: str, *, reader=None, defer=None) -> None:
        if getattr(self.store, 'activity_paused', False) or getattr(self.store, '_read_only', False):
            return
        reader = self.store if reader is None else reader
        try:
            epoch = _epoch(reader, 'cards')
            card_epochs = {p['lane']: _epoch(reader, p['lane']) for p in protos}
            if defer is None:
                home_audit.record(reader, protos, now, source=source, epoch=epoch, card_epochs=card_epochs)
            else:
                defer('offer audit', self._audit_served_menu, protos, now, source, epoch, card_epochs)
        except Exception:
            self.logger.warning("Home offer audit failed", exc_info=True)

    def _audit_served_menu(self, protos, now, source, epoch, card_epochs):
        with self.store.read_view() as reader:
            home_audit.record(reader, protos, now, source=source, epoch=epoch, card_epochs=card_epochs)

    def cards(self, request: CardsRequest) -> CardsResult:
        store, now = self.store, request.now
        from yt_playlist.rec import group_home, mode_surfaces
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
        if prepared is None and not vibe:
            distributions = (self.selection_data or {}).get('library_distributions')
            if distributions and any(distributions):
                prepared = {'dists': distributions, 'vibe_rank': home_vibe.ranker(store, now)}
        checkpoint = (prepared or {}).get('checkpoint')
        reads = getattr(store, '_snapshot_reads', None)
        batch = None
        if self.reuse_mix_candidates and not vibe:
            version = store.recommendations.catalog_read_version()
            if self._mix_batch is None or self._mix_batch_version != version:
                with (reads.capture() if reads is not None else nullcontext(())) as dependencies:
                    self._mix_batch = self._load_mix_batch(now, prepared)
                self._mix_batch_version = version
                self._mix_batch_dependencies = dependencies
            batch = self._mix_batch
            groups, group_dependencies = batch.groups, self._mix_batch_dependencies
        else:
            with (reads.capture() if reads is not None else nullcontext(())) as group_dependencies:
                groups = group_home.inputs(store, now, prepared)
        meta = groups['meta'] if groups else mode_surfaces.bundle_metadata(store)
        if checkpoint is not None and groups:
            # The published inputs are stable across retries, but live allocation can expire
            # or move with listening. Reuse the weights already needed for selection.
            checkpoint = (*checkpoint, groups['uses_preview'], groups['recommendation_generation'],
                          tuple(sorted(groups['allocation'].items())))
        group_key = ('home-group-cards', checkpoint, epoch, tuple(sorted(include)), tuple(sorted(avoid)))

        def select():
            if reads is not None:
                reads.include(group_dependencies)
            return _group_cards(store, now, epoch, groups, include, avoid, batch)

        with (reads.capture() if reads is not None else nullcontext(())) as selection_dependencies:
            groups = store.memoized_read(group_key, select) if checkpoint is not None else select()
            cards = groups['cards'] if groups else []
            if vibe and not cards:
                from yt_playlist.rec import vibe_serving
                cards = vibe_serving.assemble(store, now, vibe, epoch, include_genres=include, avoid_genres=avoid)
        if include:
            cards = [c for c in cards if c["lane"] != "fresh"]
        card_builder = _carded
        if avoid and not include and not cards and not vibe:
            used_themes = {"genres": set(), "eras": set()}

            def card_builder(reader, lane, label, pool, now, preparation=None):
                proto = _carded(reader, lane, label, pool, now, preparation, theme_avoid=used_themes)
                if len(proto['tracks']) >= 4:
                    for facet, used in used_themes.items():
                        used.update(proto['recipe']['facets'].get(facet, ()))
                return proto

        if cards:
            protos = []
            completion = prepared
            if completion is None and not vibe:
                from yt_playlist.rec.taste_analysis import era_distribution, taste_breadth
                completion = {'dists': (taste_breadth(store)['families'], dict(era_distribution(store)))}
            catalog, catalog_dependencies = None, ()
            for card in cards:
                key = ('home-complete-card', checkpoint, epoch, card['lane'], card.get('group_version'),
                       card.get('mode_id'), tuple(t['key'] for t in card['tracks']))
                def complete():
                    nonlocal catalog, catalog_dependencies
                    if catalog is None:
                        with (reads.capture() if reads is not None else nullcontext(())) as dependencies:
                            catalog = _completion_catalog(store, cards, known_metadata=bool(vibe))
                        catalog_dependencies = dependencies
                    if reads is not None:
                        reads.include(selection_dependencies)
                        reads.include(catalog_dependencies)
                    return _complete_card(store, card, now, epoch, completion,
                        {**(self.selection_data or {}), 'journey_catalog': catalog})

                protos.append(store.memoized_read(key, complete) if checkpoint is not None else complete())
            if request.record:
                self.record_modes(protos, epoch, now)
        else:
            from yt_playlist.rec import stored_home
            protos = ([] if vibe else stored_home.unmodeled_cards(store, now, include=include, avoid=avoid,
                                    preparation=prepared, card_builder=card_builder, batch=batch))
        if filters and not protos and not include and not vibe:
            return self.cards(replace(request, use_genre_request=False,
                              genre_notice="No other genre themes are ready yet. Here are your usual mixes."))
        parameters = {"include_genres": sorted(include), "avoid_genres": sorted(avoid)}
        pool_job = self.jobs.latest("fresh_pool", parameters)
        if include:
            protos = [p for p in protos if p["lane"] != "fresh"]
        if not any(p["lane"] == "fresh" for p in protos):
            if fresh := self.built_fresh(parameters, now, mix_candidates=batch.candidates if batch else None,
                                         card_builder=card_builder, preparation=prepared):
                protos.append(fresh)
        if batch is not None:
            tracks = [track for proto in protos for track in proto['tracks']]
            store.recommendations.explain_mix(tracks)
            missing = batch.candidates.load_display(tracks)
            if missing:
                for proto in protos:
                    proto['tracks'] = [track for track in proto['tracks']
                                       if ordering._field(track, 'recording_id') not in missing]
                protos = [proto for proto in protos if len(proto['tracks']) >= 4]
        fourth = ("comfort" if meta.get("comfort_pool", 0) >= rec_params.get_param(store, "comfort_min_pool")
                  or not meta.get("year_cuts") else "temporal")
        fourth = next((p["lane"] for p in protos if p["lane"] in ("comfort", "temporal")), fourth)
        genre_options = self.options()
        slots = []
        for lane in ("wheelhouse", "explore", "fresh", fourth):
            proto = next((p for p in protos if p["lane"] == lane), None)
            scoped = parameters if lane == "fresh" else {**parameters, "lane": lane, "epoch": epoch}
            if proto is None and lane != "fresh":
                excluded = [ordering._field(t, "key") for p in protos for t in p["tracks"]]
                proto = self.built_library(scoped, now, excluded, card_builder=card_builder)
                if proto:
                    protos.append(proto)
            job = pool_job if lane == "fresh" else self.jobs.latest("library_pool", scoped)
            slots.append({"proto": proto, "pool": None if proto else self.pool_view(
                scoped, job, genre_options=genre_options)})
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
            if request.version_policy is not None:
                proto['recipe']['version_policy'] = deepcopy(request.version_policy)
            proto["experiment_id"] = experiment_id(proto)
            proto["recipe"] = {**(proto.get("recipe") or {}), "experiment_id": proto["experiment_id"]}
        return CardsResult(protos, slots, notice, notice if not filters else None,
                           genre_options, sorted(genres.get('include_genres', [])) if saved else [], epoch, vibe,
                           choice_context=groups['context'] if groups else None)

    def record_modes(self, protos, epoch, now):
        legacy = [(p["lane"], p["mode_id"], p.get("ranker"))
                  for p in protos if p.get("mode_id") is not None]
        if not legacy:
            return
        try:
            self.store.modes.log_impressions(epoch, legacy, now)
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

    def built_library(self, parameters, now, exclude_keys, *, card_builder=None):
        from yt_playlist.rec import library_pool
        tracks = library_pool.pick(self.store, now, parameters, exclude_keys)
        if len(tracks) < 4:
            return None
        proto = (card_builder or _carded)(self.store, parameters["lane"], self.pool_label(parameters), tracks, now)
        if len(proto["tracks"]) < 4:
            return None
        proto["mode_id"] = None
        include, avoid = parameters["include_genres"], parameters["avoid_genres"]
        if include or avoid:
            proto["recipe"]["genre_request"] = ({"kind": "chosen_genres", "include": include} if include
                                                else {"kind": "other_genres", "avoid": avoid})
        return proto

    def built_fresh(self, parameters, now, *, mix_candidates=None, card_builder=None, preparation=None):
        include, avoid = parameters["include_genres"], parameters["avoid_genres"]
        if preparation is None:
            preparation = (self.selection_data or {}).get('preparation')
        if vibe := home_vibe.current(self.store, now):
            from yt_playlist.rec import vibe_pools
            tracks = vibe_pools.pick(self.store, now, vibe, 'fresh',
                                     include_genres=include, avoid_genres=avoid)
            if len(tracks) < 4:
                return None
            proto = _carded(self.store, 'fresh', _CARD_LABELS['fresh'], tracks, now,
                            preparation=preparation, vibe_selected=True)
            if include or avoid:
                proto['recipe']['genre_request'] = ({'kind': 'chosen_genres', 'include': include} if include
                                                    else {'kind': 'other_genres', 'avoid': avoid})
            return proto
        if include:
            from yt_playlist.rec import fresh_pool
            tracks = fresh_pool.pick(self.store, now, include, avoid,
                                     bundles=(preparation or {}).get('bundles'), mix_candidates=mix_candidates)
            if not tracks:
                return None
            proto = _proto(self.store, "fresh", _CARD_LABELS["fresh"], tracks, now)
            proto["recipe"] = recipes.roll_recipe(self.store, "fresh", seed=f"fresh:{_epoch(self.store, 'fresh')}", now=now,
                leans={}, dists=None if preparation is None else preparation['dists'])
            proto["recipe"]["facets"] = {"genres": include}
        else:
            proto = _one_card(self.store, "fresh", now, preparation=preparation, mix_candidates=mix_candidates,
                              avoid_genres=avoid, card_builder=card_builder)
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
        waiting = getattr(job, 'outcome', None) == 'waiting'
        if (done or waiting) and not job.error:
            proto = (self.built_fresh(job.parameters, now) if job.kind == "fresh_pool"
                     else self.built_library(job.parameters, now, job.exclude_keys))
            if waiting:
                head = self.store.recommendations.head('fresh')
                prepared = (head and head['id'] != job.result['generation']
                            and head['created_at'] >= job.result['requested_at'])
                done = bool(proto or prepared)
                if done:
                    job.outcome = 'completed'
                    job.done = True
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
