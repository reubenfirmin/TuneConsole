"""Home discovery and revisit summaries, offer capture, and legacy menu storage.

The cache contains complete offers, not fitted models. Selection evidence stays in
the existing mode ledger and survives replacing or losing these prepared contents.
"""
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import logging
import math
import random
import time

from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.read_memo import preparation_memo
from yt_playlist.core import artists as artist_identity, fao, home_revisit_progress, json_cache, mix_choice_policy
from yt_playlist.core.work_diagnostics import maintenance_job
from yt_playlist.rec import (album_choices, artist_start, choice_evidence, discover, fresh_pool, home_cache, home_cards, home_vibe,
                             mode_eval, mode_surfaces, rec_params, scoring, surfaces, taste_analysis,
                             transient, mix_tuning)
from yt_playlist.rec import group_serving, group_viz, musical_character, stored_home
from yt_playlist.rec.eligibility import comfort_tracks

SURFACE = "home_prepared"
DISCOVERY_SURFACE = "home_discovery"
REVISIT_SURFACE = "home_revisit"
REVISIT_PHASE = 'rec: Home revisit'
SCHEMA = json_cache.HOME_MENU_SCHEMA
MAX_MENUS = 12
MAX_AGE_S = 1800
MAX_BYTES = 4_000_000
PREPARE_BUDGET_S = 10
SUMMARY_BUDGET_S = 2
_LOG = logging.getLogger(__name__)
_CAPACITY_READERS = {surface: json_cache.DecodeCache() for surface in (SURFACE, 'home_tuned', 'home_choice')}
# Home rechecks catalog eligibility when serving. Model definitions, feedback
# and explicit controls still invalidate a preparation captured before them.
_PREPARATION_CHURN_TABLES = frozenset({
    'recordings', 'youtube_items', 'track_sources', 'library_memberships',
    'artists', 'artist_aliases', 'metadata_observations',
    'entry_artists', 'track_artists', 'playlists', 'playlist_items', 'playlist_provenance',
    # Calendar confirmation now writes the timezone on the account row.
    'accounts', 'play_events', 'player_events', 'listening_observations',
})


class _NoJobs:
    def latest(self, kind, parameters):
        return None


ROTATING_CARDS = ("wheelhouse", "explore", "comfort", "fresh", "new_artists", "discover",
                  "rediscover", "into_recently", "cards")
MENU_MAX_AGE_S = 86400


def record_visit(store, now, cards=ROTATING_CARDS):
    """Count an offer and advance rotation for a subsequent visit."""
    with store._lock:
        started = store.modes.epoch_started_at(home_cards._epoch(store, 'cards')) if 'cards' in cards else None
        refreshes = {'cards': rec_params.get_param(store, 'erosion_view_cap')} if (
            started is not None and now - started >= MENU_MAX_AGE_S) else {}
        store.rec_ledger.record_home_visit(cards, now, refreshes=refreshes)


def with_launch_playlist(store, bootstrap, launch_id):
    """Attach a requested playlist's tracks to the radio bootstrap for an in-place launch."""
    if launch_id is None:
        return bootstrap
    source = store.playlists.get_playlist(int(launch_id)) if launch_id.isdecimal() and len(launch_id) < 16 else None
    if source is not None:
        bootstrap["launchPlaylist"] = {"id": source.id, "title": source.title,
            "tracks": [track for track in store.charts.playlist_tracks_detail(source.id)
                       if track.get("video_id") and track.get("available") != 0][:8]}
    return bootstrap


def recap_notice(store, now):
    """Monthly recap ("Your <Month>") nag: once a calendar month is baked into the rollup,
    surface it as a nag card (gated on a completed first sync) linking into the full-screen
    reel. Shown until viewed or dismissed for that month; next month's recap re-shows. Expires
    at the next rollover even if the worker hasn't rebuilt yet, or recent listening is missing.
    """
    from yt_playlist.rec.trend_statistics import last_complete_month
    story = (store.rec_proposals.get_proposals("trend_rollups") or {}).get("story")
    if (story and story.get("month") == last_complete_month(now)
            and store.settings.get_setting("last_sync_at") is not None
            and store.settings.get_setting("recap_viewed_month") != story.get("month")
            and store.settings.get_setting("recap_dismissed_month") != story.get("month")):
        return {"month": story["month"], "month_name": story["month_name"]}
    return None


def card_record_gate(path, *, activity_paused, read_only, mixes_ready):
    """Does this request's assembled board get committed into the serving ledger?

    `mixes_ready` is a zero-arg callable, invoked only for "/" once the cheaper
    checks pass, matching the prior short-circuit (it may itself compute and
    cache an access snapshot, which is not needed otherwise).
    """
    if path not in ("/", "/home/cards", "/home/refresh-cards", "/home/vibe"):
        return False
    if activity_paused or read_only:
        return False
    if path == "/":
        return mixes_ready()
    return True


def polling_schedule(attempt, *, live, waiting, vibe_pending):
    """Pure: how long before Home polls again while content prepares.

    Preparation can outlast the first minute after a restart. Keep checking at
    a bounded rate until usable offers arrive instead of stranding the board
    after ten requests.
    """
    next_attempt = min(10, attempt + 1) if not live and waiting and not vibe_pending else None
    interval = min(30, 3 * 2 ** min(attempt, 4))
    return next_attempt, interval


def with_starter_fallback(store, result, now):
    """Substitute the artist-choice starter set, in place, when nothing else is ready yet."""
    if result.protos or home_vibe.current(store, now):
        return result
    first = artist_start.first_set(store, now)
    if first:
        result.protos = [first]
        result.slots = [{'proto': first}]
        result.preparing = True
        result.choice_context = {'source': 'explicit_artist_choices',
                                 'artists': first['recipe']['artists']}
    return result


def present_mix_choices(writer, result, payload, now, *, reader=None, logger=None, defer=None):
    """Issue a choice-evidence offer for the currently assembled board, in place."""
    frozen_context = (result.choice_context if result.choice_context is not None
                      else (payload or {}).get('choice_context'))
    if not (result.protos and frozen_context is not None):
        return
    for proto in result.protos:
        if proto.get('experiment'):
            proto['experiment']['ranker_controlled'] = False
    displayed = [slot.get('proto') for slot in result.slots] if result.slots else result.protos
    options = [{"id": p["experiment_id"], "position": position,
                "label": p["label"], "note": p["note"], "intent": p["lane"],
                "genres": list(p.get('genres', [])),
                "assignment": p.get("assignment")} for position, p in enumerate(displayed, 1) if p]
    try:
        oid = choice_evidence.present(writer, "mixes", options, now,
            context=frozen_context, window=result.epoch, vibe=result.vibe or None, reader=reader, defer=defer)
        for proto in result.protos:
            proto["recipe"] = {**proto.get("recipe", {}), "opportunity_id": oid,
                               "option_id": proto["experiment_id"]}
    except Exception:
        if logger is not None:
            logger.warning("Mix choice capture failed", exc_info=True)


def tuning_key(store, now=None, *, adaptive=None):
    now = time.time() if now is None else now
    mix_tuning.expire(store, now)
    # Compose the inputs through one reusable reader.
    owned = getattr(store._lock, 'held_by_current_thread', False)
    with (nullcontext(store) if owned else store.read_view()) as reader:
        vibe = home_vibe.current(reader, now)
        return reader.memoized_read(('home-tuning', now, vibe, adaptive),
            lambda: _tuning_key(reader, now, vibe, adaptive))


def _tuning_key(store, now, vibe, adaptive=None):
    from yt_playlist.rec.artist_variety import VERSION as artist_policy
    from yt_playlist.rec import candidate_refresh
    genres = store.rec_proposals.get_proposals("home_genre_request") or {}
    if not mix_tuning.genre_active(store, genres):
        genres = {}
    if adaptive is None:
        adaptive = store.taste_groups.has_adaptive_head()
    controls = {"artist_policy": artist_policy, "musical_policy": musical_character.VERSION,
                "vibe_policy": home_vibe.VERSION, "group_policy": 'adaptive' if adaptive else 'legacy',
                "vibe": vibe, "include": genres.get("include_genres", []),
                "avoid": genres.get("avoid_genres", [])}
    if vibe:
        # The board uses published ranks and current eligibility. Background
        # listening and album preferences do not identify an explicit request.
        controls['adjusted_at'] = store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING)
        controls['ranking'] = candidate_refresh.ranking_configuration(store)
        return _digest(controls)
    return _digest({**controls, "leans": store.rec_model.get_leans(),
                    "preferences": candidate_refresh.preferences(store, now, include_exposure=False),
                    "album_feedback": album_choices.feedback_state(store),
                    "breadth": rec_params.get_param(store, "breadth_bias")})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def _proposal_generation(reader):
    heads = reader.recommendations.mix_head_ids()
    group_id = reader.taste_groups.head_id()
    preview = reader.rec_proposals.group_checkpoint()
    preview_id = preview['publication_id'] if preview else None
    return ':'.join(str(identity or 0) for identity in (heads.get('home'), heads.get('fresh'), group_id, preview_id))


def prepare(store, now):
    """Prepare independent summaries, then a bounded mix batch."""
    # Discovery remains usable while a model change invalidates the mix menus,
    # or a large mix assembly spends its entire preparation budget.
    prepare_discovery(store, now)
    prepare_revisit(store, now)
    taste_analysis.prepare_library_summary(store, now)
    return prepare_mixes(store, now)


def prepare_mixes(store, now):
    """Finish an admitted menu before yielding to the next worker batch."""
    result = None
    try:
        # _prepare checks elapsed time between complete menus and publishes the
        # accumulated batch. Interrupting its SQL/setup or a menu loses all of
        # that work; elapsed time must never cancel those atomic units.
        result = _prepare(store, now)
    except WorkBudgetExceeded:
        # Quota/admission failures retain the last published menu. Elapsed
        # preparation time itself does not raise this exception anymore.
        if result is None:
            store.maintenance_diagnostics.record('deferred', reasons=('time_budget',), phase='rec: Home collections')
            _LOG.debug('Home preparation deferred: total time budget exhausted; keeping previous collections')
            _LOG.debug('Home preparation budget boundary', exc_info=True)
    return result


def load_discovery(store):
    """The last bounded display pool survives disposable mix/model projections."""
    try:
        payload = store.rec_proposals.get_proposals(DISCOVERY_SURFACE)
        if not isinstance(payload, dict):
            return None
        for field, identity, limit in (('discovery_albums', 'browse_id', 60),
                                       ('discovery_artists', 'artist_id', 40),
                                       ('discovery_tracks', 'key', 40)):
            rows = payload.get(field)
            if not isinstance(rows, list) or len(rows) > limit or any(
                    not isinstance(row, dict) or identity not in row for row in rows):
                return None
            for row in rows:
                value = row[identity]
                if (identity == 'artist_id' and (type(value) is not int or value <= 0)
                        or identity != 'artist_id' and (not isinstance(value, str) or not value)
                        or not isinstance(row.get('artist'), str)
                        or any(row.get(k) is not None and not isinstance(row[k], str)
                               for k in ('genre', 'thumbnail'))):
                    return None
        return payload
    except (ValueError, TypeError):
        return None


def prepare_discovery(store, now):
    """Publish discovery separately, without waiting for complete mix menus."""
    published = False
    try:
        with store.read_snapshot(memo=preparation_memo(store, DISCOVERY_SURFACE)) as snapshot:
            reader = snapshot.store
            previous = load_discovery(reader) or {}

            def calculate():
                leans = transient.facet_leans(reader, now)
                tracks = fresh_pool.pick(reader, now, size=40, minimum=1)
                if not (reader.recommendations.head('home') or reader.recommendations.head('fresh')):
                    tracks = previous.get('discovery_tracks', [])
                return {
                    'discovery_albums': discover.pick_discovered_albums(reader, 60, now, facet_leans=leans),
                    'discovery_artists': discover.pick_discovered_artists(reader, 40, now, facet_leans=leans),
                    'discovery_tracks': tracks,
                }

            payload = reader.memoized_read(('discovery', int(now // MAX_AGE_S)), calculate)
            encoded = json.dumps(payload, separators=(',', ':'))
            with store.rec_proposals.prepare_home(encoded, now, surface=DISCOVERY_SURFACE) as publication:
                published = snapshot.publish(lambda: store.rec_proposals.publish_home(
                        publication, input_revision=snapshot._changes),
                        allow_stale_tables=_PREPARATION_CHURN_TABLES)
    except WorkBudgetExceeded:
        if not published:
            _LOG.debug('Discovery preparation deferred; keeping the last display pool')
    return payload if published else None


def prepare_revisit(store, now):
    """Checkpoint independent history summaries without spending the mix-menu budget.

    Persist the next component after each completion. A slow component or a
    changing minute cannot make every retry repeat the same completed setup.
    """
    fields = home_revisit_progress.FIELDS
    previous = store.rec_proposals.get_proposals(REVISIT_SURFACE) or {}
    remaining = store.rec_proposals.revisit_remaining()
    if remaining is None:
        remaining = previous.get('remaining')
    remaining = remaining or list(fields)
    targets = (lambda reader: surfaces.rediscover_playlists(reader, now, count=8, pool=8),
               lambda reader: surfaces.rediscover_albums(reader, now, count=9, pool=9),
               lambda reader: surfaces.rediscover_tracks(reader, now))
    try:
        completed = True
        with maintenance_job(REVISIT_PHASE):
            for field in list(remaining):
                offset = fields.index(field)
                with store.read_snapshot(memo=preparation_memo(store, ('home-revisit', field))) as snapshot:
                    value = snapshot.store.memoized_read(('home-revisit', field, int(now // MAX_AGE_S)),
                        lambda: targets[offset](snapshot.store))

                    latest = snapshot.store.rec_proposals.get_proposals(REVISIT_SURFACE) or {}
                    next_remaining = [pending for pending in remaining if pending != field]
                    changed = latest.get(field) != value
                    payload = {key: item for key, item in latest.items() if key != 'remaining'}
                    prepared = (store.rec_proposals.prepare_home(json.dumps({**payload, field: value}), now,
                                surface=REVISIT_SURFACE) if changed else nullcontext(None))
                    with prepared as publication:
                        def publish():
                            # An unchanged component still advances the durable cursor.
                            if publication is not None:
                                store.rec_proposals.publish_home(publication, input_revision=snapshot._changes)
                            store.rec_proposals.save_revisit_remaining(next_remaining)
                            remaining[:] = next_remaining

                        if not snapshot.publish(publish, allow_stale_tables=_PREPARATION_CHURN_TABLES):
                            completed = False
        if completed:
            store.maintenance_diagnostics.record('completed', phase=REVISIT_PHASE)
    except WorkBudgetExceeded:
        # The failed component has not completed. Its predecessor's durable
        # cursor already points here; never skip it or reset completed work.
        store.maintenance_diagnostics.record('deferred', reasons=('time_budget',),
                                             phase=REVISIT_PHASE)
        return False
    return completed


def _prepare(store, now):
    started = time.monotonic()
    mix_tuning.expire(store, now)
    tuning_revision = store.home_cache.input_revision(mix_tuning.PUBLICATION_INPUTS)
    with store.read_view(preparation=True, seconds=None) as reader, stored_home.preparation_inputs() as metadata_inputs:
        target = "home_tuned" if mix_tuning.remaining(reader, now) > 0 else SURFACE
        group_head = group_viz.prepared(reader)
        adaptive = bool(group_head)
        generation = _proposal_generation(reader)
        preview = group_serving.matching_preview(reader, group_head['generation']) if group_head else None
        framing = mode_surfaces.bundle_metadata(reader)
        if (not (reader.recommendations.head('home') or reader.recommendations.head('fresh'))
                and reader.rec_query.library_keys()):
            store.maintenance_diagnostics.record('deferred', reasons=('candidate_pools',),
                                                 phase='rec: Home collections')
            return None
        tuning = tuning_key(reader, now, adaptive=adaptive)
        summary = taste_analysis.library_summary(reader)
        options = summary['genre_options']
        preferences = scoring.facet_preferences(reader, now, recent={})
        fingerprint = {"breadth": summary['breadth'], "eras": summary['eras'],
                       "genre_plays": summary['genre_plays'],
                       "transient": transient.prepared_facet_leans(reader, now)}
        context = ({} if adaptive else
            choice_evidence.model_context(reader, now, facet_leans=fingerprint['transient']))
        preparation = {'leans': {}, 'preferences': preferences,
            'metadata_inputs': metadata_inputs,
            'framing': framing,
            'group_preview': preview, 'group_head': group_head,
            'dists': (fingerprint['breadth']['families'], dict(fingerprint['eras'])),
            'vibe_rank': home_vibe.ranker(reader, now),
            'checkpoint': (generation, tuning, reader.rec_model.next_feedback_expiry(now))}
        service = home_cards.HomeCards(reader, _NoJobs(), _LOG, genre_options=options,
                                      selection_data={'preparation': preparation},
                                      reuse_mix_candidates=True)
        from yt_playlist.controllers.home_preparation import HomePreparation

        def calculate(offset):
            return service.cards(home_cards.CardsRequest(now, epoch=offset, record=False))

        def publish(encoded, published_at):
            with home_cache.writing(store):
                publication = home_cache.prepare(store, target, encoded, published_at)
                with atomic_write(store):
                    check_cancellable_work()
                    # The reader follows committed writes during assembly. A
                    # changed adjustment must leave the previous cache in place.
                    if store.home_cache.input_revision(mix_tuning.PUBLICATION_INPUTS) != tuning_revision:
                        store.maintenance_diagnostics.record('deferred', reasons=('mix_tuning',),
                                                             phase='rec: Home collections')
                        return False
                    # A slow worker must not replace a tuning result that
                    # completed while it was still assembling the menu.
                    if store.home_cache.published_after(target, published_at):
                        return False
                    home_cache.publish(store, publication)
                    check_cancellable_work()
                return True

        def freeze(value):
            return {**choice_evidence.freeze_context(reader, value),
                    'mix_display': mix_choice_policy.context(value, mixes=True)}

        commands = HomePreparation(calculate=calculate, freeze=freeze,
            is_complete=_complete_menu,
            has_complete=lambda: batch_complete(_load_surface(reader, target, readiness_only=True)),
            encode=lambda payload: json_cache.encode_home_menus(payload['menus'], _display_payload(payload)),
            publish=publish, check=check_cancellable_work, elapsed=lambda: time.monotonic() - started,
            deferred=lambda reason: store.maintenance_diagnostics.record('deferred', reasons=(reason,), phase='rec: Home collections'),
            max_menus=MAX_MENUS, budget=PREPARE_BUDGET_S, max_bytes=MAX_BYTES, schema=SCHEMA)
        with reader.recommendations.reuse_complete_ranks():
            return commands.prepare(cursor=home_cards._epoch(reader, 'cards'), adaptive=adaptive,
                now=now, tuning=tuning, generation=generation, options=options, fingerprint=fingerprint,
                collections={'choice_context': context})


def batch_complete(payload):
    """One complete board already supplies four playable Home collections."""
    return bool(payload) and any(_complete_menu(menu) for menu in payload['menus'])


def _complete_menu(menu):
    return (len(menu['protos']) == 4
        and len({proto['lane'] for proto in menu['protos']}) == 4
        and all(proto['tracks'] for proto in menu['protos']))


def has_prepared_batch(store, now):
    """Keep a fresh serving batch while background candidate pools catch up."""
    mix_tuning.expire(store, now)
    with store.read_view() as reader:
        target = 'home_tuned' if mix_tuning.remaining(reader, now) > 0 else SURFACE
        payload = _load_surface(reader, target, readiness_only=True)
        return (batch_complete(payload) and now - payload['built_at'] < MAX_AGE_S
                and payload['tuning'] == tuning_key(reader, now))


def load(store, now, schedule=lambda: None, *, display_only=False):
    """A missing/corrupt cache never falls back to synchronous ranking."""
    mix_tuning.expire(store, now)
    return peek(store, now, schedule, display_only=display_only)


def peek(store, now, schedule=lambda: None, *, readiness_only=False, display_only=False):
    """Select the serving payload without publishing or recording a menu."""
    key = tuning_key(store, now)
    fallback = None
    fallback_matches = False
    vibe = home_vibe.current(store, now)
    genres = store.rec_proposals.get_proposals('home_genre_request') or {}
    requested = genres if mix_tuning.genre_active(store, genres) else {}
    for surface in ("home_tuned", SURFACE):
        payload = _load_surface(store, surface, readiness_only=readiness_only, display_only=display_only)
        if payload is None:
            continue
        if payload.get("tuning") == key:
            if surface == 'home_tuned':
                epoch = mix_tuning.card_epoch(store)
                if all(menu['epoch'] < epoch for menu in payload['menus']):
                    replacement = _load_surface(store, SURFACE, readiness_only=readiness_only,
                                                display_only=display_only)
                    if (replacement and replacement['tuning'] == key
                            and replacement['built_at'] >= payload['built_at']
                            and type(replacement.get('rotation_epoch')) is int
                            and replacement['rotation_epoch'] >= epoch
                            and batch_complete(replacement)):
                        payload = replacement
            if now - payload["built_at"] >= MAX_AGE_S:
                schedule()
            return payload
        # Ranking freshness must not remove complete, eligible mixes. Prefer
        # the tailored set while a vibe is held, and the baseline after expiry.
        matches = bool(requested) and any(_matches_genre_request(menu['protos'], requested)
                                          for menu in payload['menus'])
        if (fallback is None or (matches and not fallback_matches)
                or (surface == SURFACE and not vibe and not fallback_matches)):
            fallback = payload
            fallback_matches = matches
    schedule()
    return fallback


def _capacity_payload(value):
    """The same menus and selection policy, without unused musical evidence."""
    if not isinstance(value, dict):
        return value
    # Validate the original complete display before dropping fields that cannot
    # affect capacity. Corrupt collections must keep the ordinary fallback.
    if 'menus' in value and not _valid(value):
        return None
    def proto(item):
        if not isinstance(item, dict):
            return item
        result = {key: child for key, child in item.items() if key in (
            'lane', 'experiment_id', 'mode_id', 'name', 'label', 'note', 'recipe', 'tracks',
            'experiment', 'counterfactual')}
        if isinstance(item.get('tracks'), list):
            result['tracks'] = [{key: child for key, child in track.items() if key in (
                                    'key', 'title', 'artist', 'video_id', 'genre')}
                                if isinstance(track, dict) else track for track in item['tracks']]
        if isinstance(item.get('recipe'), dict):
            result['recipe'] = {'genre_request': item['recipe'].get('genre_request') or {}}
        if isinstance(item.get('experiment'), dict):
            result['experiment'] = {}
        if isinstance(item.get('counterfactual'), dict):
            result['counterfactual'] = proto(item['counterfactual'])
        return result
    def menu(item):
        if not isinstance(item, dict):
            return item
        result = dict(item)
        if isinstance(item.get('protos'), list):
            result['protos'] = [proto(p) for p in item['protos']]
        if isinstance(item.get('slots'), list):
            def slot(value):
                if not isinstance(value, dict):
                    return value
                projected = proto(value.get('proto'))
                # The display normally repeats each offer in its slot. Share
                # identical projected values within this detached document so
                # copying it does not visit the same tracks twice.
                projected = next((p for p in result.get('protos', []) if p == projected), projected)
                return {**value, 'proto': projected}
            result['slots'] = [slot(value) for value in item['slots']]
        return result
    result = {key: child for key, child in value.items()
              if key not in ('album_choices', 'album_choice_context', 'play_exposure')}
    for field in ('rediscover', 'rediscover_albums', 'rediscover_tracks', 'discovery_albums',
                  'discovery_artists', 'discovery_tracks'):
        if field in result:
            result[field] = []
    if isinstance(value.get('choice_context'), dict):
        result['choice_context'] = {key: child for key, child in value['choice_context'].items()
                                   if key in ('group_generation', 'comparison')}
    if isinstance(value.get('menus'), list):
        result['menus'] = [menu(item) for item in value['menus']]
    if isinstance(value.get('menu'), dict):
        result['menu'] = menu(value['menu'])
    return result


def _capacity_read(store, surface):
    encoded = home_cache.encoded(store, surface)
    return _CAPACITY_READERS[surface].decode(encoded, project=_capacity_payload,
        cache_projection=True) if encoded is not None else None


def _display_payload(value):
    selection = _capacity_payload(value)
    if not isinstance(selection, dict) or 'menus' not in selection:
        return None
    # Selection uses menu identities, mode IDs and frozen context, rather than
    # every alternative's complete track metadata. Reuse the capacity projection
    # for those menus; restore all display and evidence fields from this exact
    # document after selecting a menu.
    if 'choice_context' in value:
        selection['choice_context'] = value['choice_context']
    return selection


class _DisplayPayload(dict):
    """Detached selection metadata with access to its exact verified document."""

    def __init__(self, value, restore):
        super().__init__(value)
        self._restore = restore

    def restore_menu(self, menu):
        index = next((i for i, candidate in enumerate(self['menus']) if candidate is menu), None)
        # A held menu already owns its complete frozen evidence.
        if index is None:
            return menu
        return self._restore(index)


def _display_read(store, surface):
    if indexed := home_cache.menu_index(store, surface):
        return _DisplayPayload(*indexed)
    # The same bounded decoder retains at most its capacity and display
    # projections for one exact document. Physical integrity is checked first.
    encoded = home_cache.encoded(store, surface)
    if encoded is None:
        return None
    decoder = _CAPACITY_READERS[surface]
    value = decoder.decode(encoded, project=_display_payload, cache_projection=True)
    return (_DisplayPayload(value, lambda index: decoder.decode(encoded, project=lambda data: data['menus'][index]))
            if value is not None else None)


def _load_surface(store, surface, *, readiness_only=False, display_only=False):
    # Read the current publication and its physical files on every request.
    # SQL revisions alone cannot detect an unavailable or damaged file.
    try:
        if readiness_only:
            payload = _capacity_read(store, surface)
            return payload if isinstance(payload, dict) and 'menus' in payload else None
        if display_only:
            return _display_read(store, surface)
        payload = home_cache.read(store, surface)
        return payload if _valid(payload) else None
    except (OSError, fao.FileChanged, ValueError, TypeError, KeyError):
        # A damaged tailored entry must not hide a healthy baseline collection.
        return None


def _valid(payload):
    def number(value):
        return isinstance(value, (int, float)) and math.isfinite(value)

    def pairs(value):
        return isinstance(value, list) and all(isinstance(p, (list, tuple)) and len(p) == 2
            and isinstance(p[0], str) and number(p[1]) for p in value)

    def track(value, *, unknown_genre=False):
        return (isinstance(value, dict)
                and all(isinstance(value.get(k), str) for k in ('key', 'title', 'artist', 'video_id'))
                and (isinstance(value.get('genre', ''), str) or (unknown_genre and value.get('genre') is None)))

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
        if (not isinstance(payload, dict) or payload["schema"] not in (1, SCHEMA)
                or not isinstance(payload.get('tuning'), str)
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
            rows = payload.get(field, [] if payload['schema'] == SCHEMA else None)
            if not isinstance(rows, list) or any(
                    not isinstance(value, dict) or identity not in value for value in rows):
                return False
        if not isinstance(payload.get('rediscover_tracks', []), list) or not all(track(t) for t in payload.get('rediscover_tracks', [])):
            return False
        tracks = payload.get('discovery_tracks', [] if payload['schema'] == SCHEMA else None)
        if not isinstance(tracks, list) or not all(track(t, unknown_genre=True) for t in tracks):
            return False
        if not isinstance(payload.get('modes', []), list) or any(
                not isinstance(m, dict) or not isinstance(m.get('mode_id'), int)
                or not number(m.get('size')) or not pairs(m.get('families', []))
                for m in payload.get('modes', [])):
            return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _cache_tracks(tracks):
    """Normalize preview objects while retaining complete prepared track evidence."""
    return [asdict(track) if isinstance(track, surfaces.ForYouItem) else track for track in tracks]


def remember(store, payload, result, now, *, tuning=None, before_publish=None, explicit_vibe=False, show=None):
    """Reuse the user's explicit tailored result on subsequent ordinary reads."""
    # A tailored rebuild is a new menu. Clear the previous menu draw before capture.
    context = deepcopy(result.choice_context if result.choice_context is not None
                       else (payload or {}).get('choice_context') or {})
    context.pop('comparison', None)
    result.choice_context = context
    if explicit_vibe and len(result.protos) == 4:
        by_lane = {p['lane']: p for p in result.protos}
        result.protos = [by_lane[slot['proto']['lane']] for slot in result.slots if slot['proto']]
    for proto in result.protos:
        proto['assignment'] = None
        if not explicit_vibe or 'experiment_id' not in proto:
            proto['experiment_id'] = home_cards.experiment_id(proto)
        proto['recipe']['experiment_id'] = proto['experiment_id']
        if proto.get('experiment'):
            proto['experiment']['assignment'] = None
            proto['experiment']['ranker_controlled'] = False
    if show is not None:
        show(result)
    # Pool slots may contain a live job with locks; only the completed offers are cached.
    menu = vars(home_cards.CardsResult(result.protos, [], result.genre_notice, result.genre_warning,
                                      result.genre_options, result.requested_genres, result.epoch, result.vibe)).copy()
    # Preview paths also produce ForYouItem objects. Normalize their tracks at
    # this persistence boundary, including shadow offers, without recursively
    # copying the large profiles already carried by prepared track dictionaries.
    menu['protos'] = []
    for proto in result.protos:
        cached = {**proto, 'recipe': dict(proto['recipe']), 'tracks': _cache_tracks(proto['tracks'])}
        if shadow := proto.get('counterfactual'):
            cached['counterfactual'] = {**shadow, 'tracks': _cache_tracks(shadow['tracks'])}
        menu['protos'].append(cached)
    if not (explicit_vibe and _complete_menu(menu)):
        by_lane = {p['lane']: p for p in menu['protos']}
        for slot in result.slots:
            if slot['proto']:
                menu['slots'].append({'proto': by_lane[slot['proto']['lane']], 'pool': None})
            elif slot['pool']:
                menu['slots'].append({'proto': None, 'pool': {**slot['pool'], 'job': None, 'building': False}})
    tailored = {**(payload or {}), "schema": 1, "built_at": now, "rotation_epoch": result.epoch,
                "group_policy": 'adaptive' if context.get('group_generation') else 'legacy',
                "tuning": tuning_key(store, now) if tuning is None else tuning, "menus": [menu],
                "genre_options": result.genre_options,
                "choice_context": context,
                "fingerprint": (payload or {}).get("fingerprint", {
                    "breadth": {"families": {}, "breadth": 0}, "eras": [], "transient": {}})}
    for field in ('rediscover', 'rediscover_albums', 'rediscover_tracks', 'discovery_albums', 'discovery_artists', 'discovery_tracks'):
        tailored.setdefault(field, [])
    if explicit_vibe:
        # The sole published menu is the menu the listener just chose.
        # A second file adds nothing to the selection or to restart recovery.
        tailored['explicit_vibe'] = True
        return home_cache.put(store, 'home_tuned', tailored, now, before_publish=before_publish)
    # An explicit tuning/build replaces the displayed menu even within the same
    # choice window. Preserve that choice across refills without attributing the
    # replacement to the earlier randomized menu assignment.
    held = {"epoch": result.epoch, "tuning": tailored["tuning"],
            "prepared_tuning": tailored['tuning'],
            "group_policy": tailored['group_policy'],
            "experiments": [p["experiment_id"] for p in menu["protos"]],
            "menu": menu, "choice_context": context,
            "assignment": None}
    # Small, explicit menus need compression, but not the large pool format's
    # repeated-profile expansion and validation pass on every publication/read.
    return home_cache.remember(store, tailored, held, now, before_publish=before_publish)


def _menu_comfort_tracks(menu):
    return [track for proto in menu['protos'] if proto['lane'] == 'comfort' for track in proto['tracks']]


def _is_explicit_menu(payload, epoch, tuning):
    return (payload.get('explicit_vibe') is True and len(payload['menus']) == 1
            and payload['menus'][0]['epoch'] == epoch and payload['tuning'] == tuning)


def _prepared_choice(store, payload, now, epoch, tuning, *, record, readiness_only=False):
    """Select one worker-produced menu without changing its prepared mixes."""
    menus = payload["menus"]
    band = epoch % 3
    candidates = [m for m in menus if m["epoch"] % 3 == band] or menus
    # Each menu was already diversified and ordered. This draw is bounded by the
    # batch size, and can react to feedback without recomputing a single track score.
    observed = None
    if _is_explicit_menu(payload, epoch, tuning):
        observed = {'epoch': epoch, 'tuning': tuning, 'group_policy': payload['group_policy'],
                    'menu': menus[0], 'choice_context': payload.get('choice_context'),
                    'experiments': [p['experiment_id'] for p in menus[0]['protos']], 'assignment': None}
    else:
        try:
            observed = (_capacity_read(store, 'home_choice') if readiness_only
                        else home_cache.read(store, 'home_choice'))
        except (TypeError, ValueError):
            pass
    held = dict(observed) if isinstance(observed, dict) and isinstance(observed.get('experiments'), list) else {}
    # A compatible held menu can survive refills while its offers stay playable.
    retained_comparison = (held.get('choice_context') or {}).get('comparison')
    same_window = (held.get('epoch') == epoch
                   and held.get('group_policy', 'legacy') == payload.get('group_policy', 'legacy'))
    same_tuning = (held.get('tuning') == tuning
                   and held.get('prepared_tuning', payload['tuning']) == payload['tuning'])
    chosen = None
    if (same_window and same_tuning and not retained_comparison
            and (held.get('menu') is None or len(held['menu']['protos']) >= max(
                len(menu['protos']) for menu in candidates))):
        chosen = held.get("menu")
        if not chosen:
            identities = set(held["experiments"])
            chosen = max(candidates, key=lambda m: sum(p["experiment_id"] in identities for p in m["protos"]))
            # Legacy held records did not freeze their menu. Its replacement has no
            # known probability, even when some experiment IDs still match.
            held["assignment"] = None
    held_comfort = _menu_comfort_tracks(chosen) if chosen else []
    eligible = {track['key'] for track in comfort_tracks(store, held_comfort, now)}
    complete = []
    if not chosen or not _complete_menu(chosen) or (held_comfort and not eligible):
        # Look across the bounded publication when the preferred band cannot
        # supply a full board. One eligibility lookup covers all Comfort offers.
        available = {track['key'] for track in comfort_tracks(store,
            [track for menu in menus for track in _menu_comfort_tracks(menu)], now)}
        complete = [menu for menu in menus if _complete_menu(menu)
                    and all(track['key'] in available for track in _menu_comfort_tracks(menu))]
        if complete or not chosen:
            eligible = available
    if not chosen or complete:
        if complete:
            candidates = [menu for menu in complete if menu['epoch'] % 3 == band] or complete
        # A replacement is a fresh whole-menu draw with its own propensity.
        modes = payload.get("modes", [])
        stats = (mode_eval.mode_bandit_stats(store, before=store.modes.epoch_started_at(epoch), now=now)
                 if modes else {})
        priorities = {m["mode_id"]: (lambda ab: ab[0] / sum(ab))(
            mode_eval.choice_posterior(*stats.get(m["mode_id"], (0, 0)))) for m in modes}
        size_power = rec_params.get_param(store, "bandit_size_power")
        priorities = {m["mode_id"]: priorities[m["mode_id"]] * max(1, m["size"]) ** size_power for m in modes}
        def score(menu):
            if payload.get('group_policy') == 'adaptive':
                # The prepared graph menus already conserve broad-area mass.
                # Applying flat legacy mode priors here would count children again.
                return 1.
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
    if isinstance(payload, _DisplayPayload):
        try:
            chosen = payload.restore_menu(chosen)
        except (OSError, fao.FileChanged, ValueError, TypeError, KeyError):
            return payload, {**chosen, 'protos': []}, [], {}, observed
    # Exposure metadata and live eligibility checks belong to the response;
    # keep the stored offer intact.
    protos = deepcopy(chosen['protos'])
    blocked = store.rec_model.disliked_identity_keys(
        keys=[track['key'] for proto in protos for track in proto['tracks']], now=now)
    # Listening can change while a prepared menu is held. A comfort card's
    # promise is eligibility, so never keep a newly heard song in that offer.
    for proto in protos:
        proto['tracks'] = [track for track in proto['tracks'] if track['key'] not in blocked]
        if proto['lane'] == 'comfort':
            proto['tracks'] = [track for track in proto['tracks'] if track['key'] in eligible]
    protos = [proto for proto in protos if proto['tracks']]
    return payload, chosen, protos, held, observed


def _matches_genre_request(protos, requested):
    return all(
        set(p.get('recipe', {}).get('genre_request', {}).get(key, [])) == set(requested.get(f'{key}_genres', []))
        for p in protos for key in ('include', 'avoid'))


def _save_served_menu(writer, remembered, now, observed_id):
    epoch = remembered['epoch']
    home_cache.put(writer, 'home_choice', remembered, now,
                   before_publish=lambda: (writer.home_cache.publication_id('home_choice') == observed_id
                                           and home_cards._epoch(writer, 'cards') == epoch))


def cards(store, payload, now, service, schedule=lambda: None, *, record=True, readiness_only=False,
          writer=None, complete_only=False, defer=None):
    """Choose an experiment from prepared alternatives using current mode evidence."""
    if (not getattr(store, '_read_only', False)
            and not getattr(store._lock, 'held_by_current_thread', False)):
        mix_tuning.expire(store, now)
        # Compose the read phase through one reporting reader. Otherwise each
        # repository lookup on the writer constructs its own reader and owners.
        # Choice/exposure writes still use the original writer and service.
        with store.read_view() as reader:
            return cards(reader, payload, now, service, schedule, record=record,
                         readiness_only=readiness_only, writer=store if writer is None else writer,
                         complete_only=complete_only, defer=defer)
    epoch = home_cards._epoch(store, "cards")
    tuning = tuning_key(store, now)
    genres = store.rec_proposals.get_proposals("home_genre_request") or {}
    requested = genres if mix_tuning.genre_active(store, genres) else {}
    vibe = home_vibe.current(store, now)
    if not payload:
        schedule()
        options = []
        requested = sorted(genres.get('include_genres', [])) if mix_tuning.genre_active(store, genres) else []
        slots = []
        for lane in ('wheelhouse', 'explore', 'fresh', 'comfort'):
            parameters = {'include_genres': requested,
                          'avoid_genres': sorted(genres.get('avoid_genres', [])) if mix_tuning.genre_active(store, genres) else []}
            if lane != 'fresh':
                parameters.update(lane=lane, epoch=epoch)
            slots.append({'proto': None, 'pool': service.pool_view(parameters, genre_options=options)})
        return home_cards.CardsResult([], slots, None, None, options, requested, epoch, vibe, preparing=True)
    choice = _prepared_choice(store, payload, now, epoch, tuning, record=record,
                              readiness_only=readiness_only)
    if len(choice[2]) < 4:
        schedule()
        # Incomplete tailored menus must not hide a usable baseline (or vice
        # versa). Replace the whole set so every card keeps its own generation's
        # frozen context. Only the selected set reaches exposure recording below.
        for surface in ('home_tuned', SURFACE):
            alternative = _load_surface(store, surface, readiness_only=readiness_only,
                                        display_only=isinstance(payload, _DisplayPayload))
            if alternative is None or alternative == payload:
                continue
            candidate = _prepared_choice(store, alternative, now, epoch, tuning, record=record,
                                         readiness_only=readiness_only)
            # A smaller usable genre selection takes precedence over unrelated
            # cards. Keep its whole menu and context while missing lanes refill.
            if (requested and choice[2] and _matches_genre_request(choice[2], requested)
                    and not _matches_genre_request(candidate[2], requested)):
                continue
            if len(candidate[2]) > len(choice[2]):
                choice = candidate
            if len(choice[2]) == 4:
                break
    payload, chosen, protos, held, observed = choice
    refreshing = payload['tuning'] != tuning
    waiting_for_rotation = ('rotation_epoch' in payload
                            and all(menu['epoch'] < epoch for menu in payload['menus']))
    if refreshing or waiting_for_rotation:
        schedule()
    for proto in protos:
        proto["assignment"] = held.get("assignment")
        if proto.get("experiment"):
            proto["experiment"]["cluster"] = f"{epoch}:{proto.get('mode_id')}"
            proto["experiment"]["assignment"] = held.get("assignment")
            # Randomizing whole menus does not isolate an individual ranker's effect.
            proto["experiment"]["ranker_controlled"] = False
    by_lane = {p["lane"]: p for p in protos}
    slots = []
    for slot in chosen['slots'] or ({'proto': p, 'pool': None} for p in chosen['protos']):
        if slot["proto"]:
            if proto := by_lane.get(slot["proto"]["lane"]):
                slots.append({"proto": proto, "pool": None})
            else:
                lane = slot['proto']['lane']
                request = slot['proto'].get('recipe', {}).get('genre_request') or {}
                parameters = {'include_genres': request.get('include', []), 'avoid_genres': request.get('avoid', [])}
                if lane != 'fresh':
                    parameters.update(lane=lane, epoch=epoch)
                kind = 'fresh_pool' if lane == 'fresh' else 'library_pool'
                pool = service.pool_view(parameters, service.jobs.latest(kind, parameters),
                                         genre_options=payload['genre_options'])
                slots.append({'proto': None, 'pool': pool})
        elif slot["pool"]:
            pool = deepcopy(slot["pool"])
            if "epoch" in pool["parameters"]:
                pool["parameters"]["epoch"] = epoch
            kind = "fresh_pool" if pool["lane"] == "fresh" else "library_pool"
            if job := service.jobs.latest(kind, pool["parameters"]):
                pool = service.pool_view(pool["parameters"], job, genre_options=payload["genre_options"])
            slots.append({"proto": None, "pool": pool})
    notice = None
    if refreshing and protos:
        genre_pending = bool(requested) and not _matches_genre_request(protos, requested)
        vibe_pending = vibe and any(p.get('recipe', {}).get('vibe') != vibe for p in protos)
        if vibe_pending or genre_pending:
            labels = {option['family']: option['label'] for option in payload['genre_options']}
            preferences = [home_vibe.PRESETS[vibe]['label'].lower()] if vibe else []
            preferences.extend(labels.get(genre, genre.removeprefix('other:'))
                               for genre in sorted(requested.get('include_genres', [])))
            avoided = [labels.get(genre, genre.removeprefix('other:'))
                       for genre in sorted(requested.get('avoid_genres', []))]
            if preferences or avoided:
                description = ' + '.join(preferences) + ' mix tapes' if preferences else 'mix tapes'
                if avoided:
                    description += ' without ' + ' + '.join(avoided)
                notice = f"Heads up - we're unable to create {description} right now."
    result = home_cards.CardsResult(protos, slots,
        None if refreshing else chosen["genre_notice"], None if refreshing else chosen["genre_warning"], payload["genre_options"],
        chosen["requested_genres"], epoch, home_vibe.current(store, now),
        choice_context=held.get('choice_context') or {},
        preparing=refreshing or waiting_for_rotation or len(protos) < 4,
        preference_notice=notice)
    if record and not waiting_for_rotation and (not complete_only or len(protos) >= 4):
        writer = store if writer is None else writer
        remembered = {"epoch": epoch, "tuning": tuning,
            "prepared_tuning": payload['tuning'],
            "group_policy": payload.get('group_policy', 'legacy'),
            "experiments": [p["experiment_id"] for p in protos],
            "menu": chosen,
            "choice_context": held.get('choice_context'),
            "assignment": held.get("assignment")}
        if _is_explicit_menu(payload, epoch, tuning):
            if any(proto.get('mode_id') is not None for proto in protos):
                if defer is None:
                    service.record_modes(protos, epoch, now)
                else:
                    defer('record menu modes', service.record_modes, protos, epoch, now)
        elif defer is None:
            if not home_cache.matches_current(writer, 'home_choice', remembered, reader=store, observed=observed):
                home_cache.put(writer, 'home_choice', remembered, now)
            if protos:
                service.record_modes(protos, epoch, now)
        else:
            if remembered != observed:
                defer('save displayed menu', _save_served_menu, writer, remembered, now,
                      home_cache.observed_publication_id(store, 'home_choice'))
            if any(proto.get('mode_id') is not None for proto in protos):
                defer('record menu modes', service.record_modes, protos, epoch, now)
    return result


def refresh_card_proto(store, payload, now, service, card, calculate):
    """Advance one card's ledger epoch and return its newly chosen proto, if any."""
    cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
    store.rec_ledger.refresh_card(card, cap, now)
    store.rec_ledger.refresh_card("cards", cap, now)
    result = calculate(payload, now)
    proto = next((p for p in result.protos if p["lane"] == card), None)
    if proto is not None:
        service.record_modes([proto], result.epoch, now)
    return proto


def fingerprint(store, payload, now):
    empty = {"breadth": {"families": {}, "breadth": 0}, "eras": [], "transient": {}}
    prepared = (payload or {}).get('fingerprint', empty)
    prepared = {**prepared, 'transient': transient.prepared_facet_leans(store, now)}
    return taste_analysis.taste_fingerprint(store, now, prepared=prepared)


def collections(store, payload, now):
    """Rotate small prepared pools, retaining current source and feedback eligibility."""
    # Collections belong to their current worker publication, independently of
    # an older tailored mix board retained for a temporary taste adjustment.
    return {**revisit(store, now, payload), **discovery(store, payload, now)}


def revisit(store, now, payload=None):
    identities = (('rediscover', 'id'), ('rediscover_albums', 'browse_id'), ('rediscover_tracks', 'key'))
    fields = tuple(field for field, identity in identities)
    revisit = store.rec_proposals.get_proposals(REVISIT_SURFACE) or {}
    if not all(field in revisit for field in fields):
        legacy = home_cache.read(store, SURFACE, fields=('schema', *fields)) or payload or {}
        revisit = {**legacy, **revisit}
    # This bounded projection bypasses full menu validation. Check its own
    # fields before rendering; a damaged collection must not break healthy
    # menus or the independent discovery/revisit publications.
    payload = _collection_fields(revisit, identities)
    epoch = max(0, store.rec_ledger.card_views('rediscover')-1)
    playlists = [{**p, **current} for p in payload.get('rediscover', [])
                 if (current := store.playlists.home_preview(p['id']))]
    saved = store.collection.saved_album_ids(browse_ids=[a['browse_id'] for a in payload.get('rediscover_albums', [])])
    recency = store.collection.saved_albums_recency(include_generated=True, browse_ids=saved)
    albums = [a for a in payload.get("rediscover_albums", []) if a["browse_id"] in saved
              and recency.get(a['browse_id']) is not None and recency[a['browse_id']] <= now-30*86400]
    tracks = surfaces.rediscover_tracks(store, now, keys=[t['key'] for t in payload.get('rediscover_tracks', [])])
    return {"rediscover": surfaces.revisit_page(playlists, 2, epoch, 'playlists'),
            "rediscover_albums": surfaces.revisit_page(albums, 3, epoch, 'albums'),
            "rediscover_tracks": surfaces.revisit_page(tracks, 15, epoch, 'songs')}


def _collection_fields(payload, identities):
    return {field: rows if isinstance(rows, list) and all(
        isinstance(row, dict) and identity in row for row in rows) else []
        for field, identity in identities if (rows := payload.get(field)) is not None}


def discovery(store, payload, now):
    identities = (('discovery_albums', 'browse_id'), ('discovery_artists', 'artist_id'), ('discovery_tracks', 'key'))
    saved = load_discovery(store) or home_cache.read(store, SURFACE, fields=(
        'schema', *(field for field, identity in identities)))
    if saved is None:
        saved = payload() if callable(payload) else payload
    payload = _collection_fields(saved or {}, identities)
    epoch = home_cards._epoch(store, "discover")
    muted_artists, unresolved_mutes = store.rec_model.artist_mute_targets(now)
    weights = store.rec_model.get_weights(now=now)
    album_pool, artist_pool = payload.get("discovery_albums", []), payload.get("discovery_artists", [])
    def visible(item):
        family, subgenre = axes.get(item.get('genre'),('',None))
        return (item.get("artist") not in unresolved_mutes and weights.get(f"genre:{family}", 1) != 0
                and weights.get(f"genre:{subgenre}", 1) != 0)
    def artist_visible(item):
        identity = item.get('artist_id')
        return identity is None or (identity not in muted_artists
                                    and artist_identity.token(identity) not in unresolved_mutes)
    album_ids, artist_ids = store.discovery.present_home_tiles(
        [a["browse_id"] for a in album_pool], [a["artist_id"] for a in artist_pool],
        recheck_artist_ownership=True)
    muted_albums = (store.rec_model.muted_album_ids([tile['browse_id'] for tile in album_ids.values()], now)
                    if muted_artists else set())
    saved = store.collection.saved_album_ids(browse_ids=[a['browse_id'] for a in album_pool])
    albums, displayed_releases = [], set()
    for candidate in album_pool:
        browse_id = candidate['browse_id']
        tile = album_ids.get(browse_id)
        if (tile is None or browse_id in saved or tile['browse_id'] in displayed_releases
                or tile['browse_id'] in muted_albums or not artist_visible(tile)):
            continue
        albums.append({**candidate, **tile})
        displayed_releases.add(tile['browse_id'])
    artists = [{**a, **tile} for a in artist_pool
               if (tile := artist_ids.get(a['artist_id'])) is not None and artist_visible(tile)]
    tracks = payload.get("discovery_tracks", [])
    # Reuse bounded offer eligibility, allowing discovery grids smaller than a mix.
    if tracks:
        keys = [t["key"] for t in tracks]
        owned = store.rec_query.tracks_by_keys(keys)
        present = store.discovery.playable_discoveries_by_keys(keys)
        blocked = store.rec_model.suppressed_keys("for_you", now, keys=keys)
        track_mutes = store.rec_model.muted_artists(now) if muted_artists else unresolved_mutes
        tracks = [{**t, **{k: present[t["key"]].get(k) for k in
                          ("title", "artist", "video_id", "thumbnail", "genre")}}
                  for t in tracks if t["key"] in present and t["key"] not in owned and t["key"] not in blocked
                  and present[t["key"]].get('artist') not in track_mutes]
    axes = store.genres.feature_axes(item.get('genre') for item in [*albums, *artists, *tracks])
    return {"discovery_albums": surfaces.rotate_page([a for a in albums if visible(a)], 15, epoch),
            "discovery_artists": surfaces.rotate_page([a for a in artists if visible(a)], 10, epoch),
            "discovery_tracks": surfaces.rotate_page([t for t in tracks if visible(t)], 10, epoch)}
