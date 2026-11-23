"""Shared request-handling context for the route modules.

Every route in :mod:`yt_playlist.web.routes` is a closure over the same handful of
collaborators (the store, the per-identity client provider, a clock, the Jinja
templates, the sync-job registry, and the optional setup runtime). Bundling them
into one object lets each router be built with ``build(ctx)`` instead of threading
six arguments through every module.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.templating import Jinja2Templates

from yt_playlist.library.ops import PlaylistOps
from yt_playlist.core.activity import Activity
from yt_playlist.web.jobs import SyncJobs

logger = logging.getLogger("yt_playlist.web")



async def request_form(request: Request):
    """Parse the body before dispatching a synchronous form handler to the request pool."""
    return await request.form()


def form_float(value):
    """Parse a form field to float, or None when it is missing or non-numeric. Lets a handler treat
    a malformed POST (a garbage slider value) as a no-op instead of raising and returning a 500."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def refresh_playlist_view(ctx, provider_playlist_id):
    """Refresh a connected extension's playlist after a confirmed mutation."""
    try:
        if provider_playlist_id:
            ctx.bridge.send_control({"type": "refresh-view", "playlist": provider_playlist_id})
    except Exception:  # noqa: BLE001 - notification failure must not change the mutation result
        ctx.logger.debug("refresh-view control not sent for %s", provider_playlist_id, exc_info=True)


@dataclass
class Ctx:
    store: object
    client_provider: object
    now_fn: object
    templates: Jinja2Templates
    jobs: SyncJobs
    work: Activity = field(default_factory=Activity)
    setup: object | None = None
    logger: logging.Logger = field(default=logger)
    # identities whose YouTube session has expired (id -> label), set during sync, cleared on
    # a successful re-sync or re-auth. Drives the "session expired, re-authenticate" banner. Seeded
    # from (and written through to) the store so the banner survives a restart; mutate ONLY via the
    # flag_/clear_ helpers below so the persisted copy stays in sync.
    auth_expired: dict = field(default_factory=dict)
    rec_worker: object | None = None   # decoupled recommendation worker (set in create_app)
    enrich_worker: object | None = None  # background enrichment worker (set in create_app)
    bridge: object | None = None       # shared Bridge instance the WS route and runtime read from
    radio: object | None = None        # saved-playlist Radio Session, set in create_app
    first_sounds: object | None = None # onboarding continuation, independent of Radio access
    updater: object | None = None         # platform updater (Flatpak portal / macOS Sparkle)
    # Guards library sync so the background sync daemon and a manual POST /sync never run at once.
    sync_lock: object = field(default_factory=threading.Lock)
    # An extension connection can start a due sync without waiting for the next periodic check.
    sync_wake: object = field(default_factory=threading.Event)
    # A Spotify Account Data import may spend hours resolving tracks through the extension. This
    # both prevents duplicate jobs and lets packaged-app idle shutdown keep the process alive.
    spotify_import_lock: object = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.auth_sessions().load()

    def auth_sessions(self):
        from yt_playlist.controllers.auth_sessions import AuthSessions
        return AuthSessions(self.store.accounts, self.auth_expired, self.logger)

    def flag_auth_expired(self, account_id, label):
        return self.auth_sessions().flag(account_id, label)

    def clear_auth_expired(self, account_id):
        return self.auth_sessions().clear(account_id)

    def clear_all_auth_expired(self):
        return self.auth_sessions().clear_all()

    def now(self):
        return self.now_fn()

    def clients(self):
        return self.client_provider()

    def update_checks(self):
        from yt_playlist.library.update_composition import checks
        return checks(self.store)

    def ops(self) -> PlaylistOps:
        """A PlaylistOps bound to this context's store, client provider, and clock."""
        return PlaylistOps(self.store, self.client_provider, self.now_fn,
                           rating_updated=self.rec_worker.trigger_rating_update if self.rec_worker else None)

    def suggestions(self):
        from yt_playlist.library.suggestion_composition import suggestions
        return suggestions(self.store, getattr(self, 'rec_worker', None), self.client_provider)

    def home_tuning(self, prepared):
        from yt_playlist.controllers.home_tuning import HomeTuning
        from yt_playlist.rec import home_prepared, mix_tuning, rec_params
        from yt_playlist.rec.home_cards import _epoch
        from yt_playlist.providers import genres
        store = self.store
        def picker():
            inputs = prepared(self.now_fn())
            return genres.picker_options(store, (inputs or {}).get('fingerprint'))

        return HomeTuning(store.settings, store.rec_model, store.rec_proposals, store.rec_ledger, store.genres,
            adjusting=lambda now: mix_tuning.adjusting(store, now),
            parameter=lambda name: rec_params.get_param(store, name),
            set_parameter=lambda name, value: rec_params.set_param(store, name, value),
            epoch=lambda: _epoch(store, 'cards'), prepared=prepared,
            expire=lambda now: mix_tuning.expire(store, now), reset=lambda: mix_tuning.reset(store),
            fingerprint=lambda now: home_prepared.fingerprint(store, prepared(now), now),
            refresh=lambda: self.rec_worker.trigger_candidate_refresh() if self.rec_worker else None,
            picker=picker)

    def cluster_commands(self):
        import logging
        from yt_playlist.controllers.clusters import Clusters
        from yt_playlist.core.activity import activity_for
        from yt_playlist.library import cluster_composition
        from yt_playlist.rec import measurement, recipes
        from yt_playlist.web.cluster_scans import ClusterScans
        store = self.store
        return Clusters(store.rec_query, store.discovery, store.playlist_recipes, store.cluster_evidence,
            store.rec_model, scans=ClusterScans(activity_for(self), store.read_snapshot),
            expand=cluster_composition.expansion, explain=cluster_composition.explanation,
            recipe=lambda *args, **kwargs: recipes.cluster_recipe(store, *args, **kwargs),
            generated=lambda: Ctx.generated_playlists(self),
            record=lambda *args, **kwargs: measurement.record_recommendations(store, *args, **kwargs),
            now=self.now_fn, logger=getattr(self, 'logger', logging.getLogger(__name__)))

    def road_trip_commands(self, drafts):
        from yt_playlist.controllers.road_trip_commands import RoadTripCommands
        from yt_playlist.core.accounts import client_for
        from yt_playlist.rec import road_trip_candidates
        return RoadTripCommands(self.store.road_trip, drafts,
            account=lambda: client_for(self.store, self.client_provider()),
            generated=lambda: Ctx.generated_playlists(self),
            artist_genre=lambda name: road_trip_candidates.artist_genre(self.store, name), logger=self.logger,
            artist_identity=self.store.artists.provider_identity)

    def suggestion_feedback(self):
        from yt_playlist.library.suggestion_composition import feedback
        return feedback(self.store)

    def discovery_actions(self):
        from yt_playlist.controllers.discovery_actions import DiscoveryActions
        from yt_playlist.core.accounts import client_for
        from yt_playlist.rec import rec_params
        return DiscoveryActions(self.store.discovery,
            retention_days=lambda: rec_params.get_param(self.store, 'discovery_gc_days'),
            account=lambda: client_for(self.store, self.client_provider()),
            rating=lambda *args, **kw: self.ops().set_rating(*args, **kw), logger=self.logger)

    def collection_commands(self):
        from yt_playlist.controllers.collection_commands import CollectionCommands
        from yt_playlist.core.accounts import AccountAccess
        from yt_playlist.rec import choice_evidence
        return CollectionCommands(self.store.collection,
            AccountAccess(self.store.accounts, self.store.playlists, self.client_provider()),
            now=getattr(self, 'now_fn', time.time),
            respond=lambda *args, **kwargs: choice_evidence.safe_respond(self.store, *args, **kwargs),
            refresh=self.rec_worker.trigger_home_refresh if getattr(self, 'rec_worker', None) else lambda: None)

    def music_search(self):
        if not hasattr(self, '_music_search'):
            from yt_playlist.controllers.music_search import MusicSearch
            from yt_playlist.controllers.catalog_library import CatalogLibrary
            from yt_playlist.core.accounts import AccountAccess
            from yt_playlist.core.db_writes import atomic_write
            from yt_playlist.rec import artist_start

            def admit(item, onboarding):
                drafting = artist_start.required(self.store)
                if drafting:
                    self.store.settings.set_setting(artist_start.DRAFT, '1')
                values = {key: value for key, value in item.items() if key not in ('duration',)}
                # Metadata files reserve and publish in their own transactions.
                # A pending save can resume this idempotent admission after a crash.
                self.store.tracks.upsert_track(**values, available=True, created_at=self.now_fn())
                if drafting:
                    with atomic_write(self.store):
                        artist_start.remember_song(self.store, item['video_id'], self.now_fn())
                if self.rec_worker:
                    self.rec_worker.trigger_mode_refresh()

            access = AccountAccess(self.store.accounts, self.store.playlists, self.client_provider)

            def publish(kind, browse, account, client, detail):
                if kind == 'album':
                    from yt_playlist.library.album_writes import saved_album
                    self.store.collection.add_saved_album(saved_album(browse, detail), account_id=account)
                elif kind == 'playlist':
                    from yt_playlist.library.sync_composition import synchronization
                    synchronization(self.store).refresh(account, client, browse.removeprefix('VL'),
                                                        detail['title'], self.now_fn())
                else:
                    self.store.artists.ensure_artist(detail['name'], provider='youtube', provider_id=browse)
                if self.rec_worker:
                    self.rec_worker.trigger_mode_refresh()

            entities = CatalogLibrary(access, self.store.actions, now=self.now_fn, publish=publish, logger=self.logger)
            self._music_search = MusicSearch(
                access,
                self.store.tracks, self.store.actions, now=self.now_fn, admit=admit, logger=self.logger,
                withdraw=lambda item: self.store.tracks.remove_song_library_membership(item['video_id']),
                entities=entities, collection=self.store.collection)
        return self._music_search

    def library_actions(self):
        from yt_playlist.controllers.library_actions import LibraryActions
        from yt_playlist.library.live_plays import resolve_identity
        from yt_playlist.rec import actions, choice_evidence, measurement
        store = self.store
        return LibraryActions(store.playlists, store.accounts, store.charts, store.tracks,
            store.playlist_recipes, store.player_events, store.enrichment_queue,
            ops=self.ops(), clients=self.clients, now=self.now_fn, logger=self.logger,
            measure=lambda *args, **kwargs: measurement.record_action(store, *args, **kwargs),
            respond=lambda *args, **kwargs: choice_evidence.safe_respond(store, *args, **kwargs),
            refresh_player=lambda provider_id: refresh_playlist_view(self, provider_id),
            refresh_cleanup=lambda: actions.refresh_cleanup(store, self.now_fn()),
            refresh_content=self.rec_worker.trigger_content_update if self.rec_worker else lambda **kwargs: None,
            wake_enrichment=self.enrich_worker.trigger if self.enrich_worker else lambda: None,
            identity=lambda: resolve_identity(store, ''))

    def action_queries(self):
        from yt_playlist.controllers.action_queries import ActionQueries
        return ActionQueries(self.store.read_view)

    def album_commands(self):
        from yt_playlist.controllers.album_commands import AlbumCommands
        from yt_playlist.core.accounts import main_client, catalog_client
        from yt_playlist.library.album_fetch import fetch_album, is_upload_album
        from yt_playlist.library.album_catalog import remember_album
        from yt_playlist.rec import album_choices, album_notes, choice_evidence, home_cache
        from yt_playlist.web.album_art import cover_and_mosaic
        store = self.store
        def client(browse):
            clients = self.client_provider()
            return main_client(store, clients) if is_upload_album(browse) else catalog_client(clients)
        return AlbumCommands(store.collection, store.tracks, store.charts, store.genres,
            store.enrichment_reports, store.album_notes, store.rec_ledger, self.jobs,
            client=client, fetch=fetch_album, remember=lambda *args: remember_album(store, *args),
            now=self.now, paused=lambda: bool(getattr(store, 'activity_paused', False)),
            respond=lambda *args, **kwargs: choice_evidence.safe_respond(store, *args, **kwargs),
            present=lambda *args, **kwargs: choice_evidence.present(store, *args, **kwargs),
            feedback=lambda *args: album_choices.set_feedback(store, *args),
            feedback_state=lambda *args: album_choices.feedback_state(store, *args),
            prepared=lambda: home_cache.read(store, 'home_prepared'),
            read_notes=lambda *args: album_notes.read(store, *args),
            refresh=self.rec_worker.trigger_home_refresh if self.rec_worker else lambda: None,
            player=lambda: self.bridge, cover=cover_and_mosaic, logger=self.logger)

    def enrichment_configuration(self):
        from yt_playlist.controllers.enrichment_configuration import EnrichmentConfiguration
        return EnrichmentConfiguration(self.store.settings, self.store.genres,
            wake=self.enrich_worker.trigger if self.enrich_worker else lambda: None)

    def enrichment_commands(self):
        from yt_playlist.controllers.enrichment import EnrichmentCommands
        from yt_playlist.core.activity import activity_for
        from yt_playlist.providers import waterfall
        store = self.store
        return EnrichmentCommands(store.enrichment_reports, store.enrichment_queue, store.enrichment_state,
            store.enrichment, store.tracks, store.settings, now=self.now_fn, worker=self.enrich_worker,
            refresh=self.rec_worker.trigger_content_update if self.rec_worker else lambda **kwargs: None,
            jobs=self.jobs, logger=self.logger, start_thread=activity_for(self).start_thread, playlists=store.playlists,
            start_enrichment=lambda **kwargs: waterfall.start_or_rejoin_enrichment(self.jobs, store, self.logger,
                activity_for(self).start_thread, **kwargs))

    def generated_playlists(self):
        from yt_playlist.controllers.generated_playlists import GeneratedPlaylists
        from yt_playlist.core.accounts import AccountAccess
        from yt_playlist.library import playlist_generated, playlist_navigation
        from yt_playlist.rec import choice_evidence, measurement
        from yt_playlist.web.jobs import start_playlist_enrichment
        def navigate(client, remote_id, intent):
            if self.bridge is not None and self.bridge.connected:
                self.work.start_thread(playlist_navigation.navigate_when_ready,
                    args=(client, self.bridge, remote_id, f'https://music.youtube.com/watch?list={remote_id}'),
                    kwargs={'intent': intent})
        return GeneratedPlaylists(AccountAccess(self.store.accounts, self.store.playlists, self.clients()),
            self.jobs, self.store.modes, now=self.now_fn,
            create=lambda *args, **kwargs: playlist_generated.create_generated_playlist(self.store, *args, **kwargs),
            query=lambda account, remote: playlist_generated.saved_playlist_query(
                self.store, self.jobs, self.client_provider, account, remote),
            measure=lambda *args, **kwargs: measurement.record_action(self.store, *args, **kwargs),
            respond=lambda *args, **kwargs: choice_evidence.safe_respond(self.store, *args, **kwargs),
            enrich=lambda pid: start_playlist_enrichment(self, pid), navigate=navigate,
            refresh=self.rec_worker.trigger_home_refresh if self.rec_worker else lambda: None,
            recipes=self.store.playlist_recipes,
            mix_playback=self.store.recommendations.mix_playback_details,
            begin_intent=self.bridge.begin_playback_intent if self.bridge is not None else lambda: None)

    def playlist_queries(self):
        from contextlib import nullcontext
        from yt_playlist.controllers.playlist_queries import PlaylistQueries
        from yt_playlist.core.accounts import AccountAccess
        from yt_playlist.library.playlist_positions import pending_insertions
        from yt_playlist.rec import measurement, onboarding, rec_params, taste_analysis
        return PlaylistQueries(lambda: self.store.read_view() if self.store._db_path is not None else nullcontext(self.store),
            AccountAccess(self.store.accounts, self.store.playlists, self.client_provider), self.jobs,
            now=self.now_fn, measure=lambda *args, **kwargs: measurement.record_action(self.store, *args, **kwargs),
            facets=taste_analysis.playlist_facets, mood=taste_analysis.playlist_mood_state,
            moods=taste_analysis.track_mood_states, pending=pending_insertions,
            initial_setup=onboarding.initial_setup_pending, gc_days=lambda reader: rec_params.get_param(reader, 'generated_gc_days'))

    def cleanup(self):
        from yt_playlist.controllers.cleanup import Cleanup
        def capture():
            with self.store.read_view() as reader:
                return reader.playlists.cleanup_memberships(), reader.overlaps.cleanup_preferences()
        return Cleanup(self.store.overlaps, self.store.accounts, capture=capture,
            publish=lambda payload, now: self.store.rec_proposals.put_proposals('cleanup', payload, now),
            paused=lambda: self.store.activity_paused, now=self.now_fn)

    def radio_commands(self):
        from yt_playlist.controllers.radio_commands import RadioCommands
        from yt_playlist.rec import access, onboarding
        return RadioCommands(self.radio, self.bridge, self.store.settings,
            completed=lambda: onboarding.setup_completed(self.store),
            require=lambda feature, **kwargs: access.require(self.store, feature, now=self.now_fn(), **kwargs),
            logger=getattr(self, 'logger', logger))

    def transfer_commands(self):
        from yt_playlist.controllers.transfer_commands import TransferCommands
        return TransferCommands(self.store.playlists, self.store.accounts, move=self.ops().move,
            refresh=lambda remote: refresh_playlist_view(self, remote), logger=self.logger)

    def merge_drafts(self):
        from yt_playlist.controllers.merge_drafts import MergeDrafts
        return MergeDrafts(self.store.playlists, self.store.accounts, self.store.actions,
                           apply=self.ops().apply_merge, now=self.now_fn)

    def playback_observations(self):
        from yt_playlist.library.playback_composition import observations
        return observations(self.store, getattr(self, 'rec_worker', None))

    def audio_capture(self, *, choices=None):
        from yt_playlist.controllers.audio_capture import AudioCapture
        from yt_playlist.core.features import audio_vibes_enabled
        return AudioCapture(self.store.audio_vibes, publication=self.store._lock,
            enabled=audio_vibes_enabled, now=self.now, choices=choices)

    def tv_commands(self):
        from yt_playlist.controllers.tv_commands import TVCommands
        from yt_playlist.controllers.tv_queries import playlist_items
        from yt_playlist.core.accounts import AccountAccess
        from yt_playlist.core.activity import activity_for
        from yt_playlist.library import tv_launch, tv_stream
        from yt_playlist.rec import tv, tv_artwork, tv_visual
        inspector = tv_visual.Inspector()
        def contents(pid, *, artwork=True):
            with self.store.read_view() as reader:
                return playlist_items(reader, pid, artwork=artwork, images=tv_artwork.images)
        refresh = lambda: self.rec_worker.trigger_tv_refresh() if self.rec_worker else None
        work = activity_for(self)
        return TVCommands(self.store.playlists, self.store.playlist_recipes, self.store.accounts,
            self.store.settings, self.store.tv, self.store.rec_proposals, contents=contents,
            status=lambda: tv.status(self.store, self.now_fn(), trigger_refresh=refresh),
            queue=lambda **kwargs: tv.queue(self.store, self.now_fn(), **kwargs),
            preparation_message=lambda: tv.preparation_message(self.store, self.now_fn()),
            cached=lambda: tv.cached(self.store), artwork=lambda artist: tv_artwork.images(self.store, artist),
            request_artwork=lambda artist: tv_artwork.request(self.store, artist, self.now_fn()),
            inspect=lambda client, item, now: inspector.inspect(self.store, client, item, now),
            access=AccountAccess(self.store.accounts, self.store.playlists, self.client_provider),
            start=lambda *args: work.wrap('start TV session', tv_launch.start, self.store, self.client_provider(), *args)(),
            refill=lambda *args: work.wrap('continue TV playlist', tv_stream.refill, self.store, *args)(), refresh=refresh,
            observe=lambda *args: work.wrap('TV playback event', Ctx.playback_observations(self).observe, *args)(),
            player=self.bridge, paused=lambda: self.store.activity_paused,
            now=self.now_fn, logger=self.logger)

    def playback_commands(self, *, monotonic=time.monotonic, reader=None):
        from yt_playlist.controllers.playback_commands import PlaybackCommands
        from yt_playlist.core import playback_feedback
        from yt_playlist.library import live_plays, ratings
        from yt_playlist.rec import measurement
        from yt_playlist.core.activity import activity_for
        store = getattr(self, 'store', None) if reader is None else reader
        def duration(video):
            with store.read_view() as reader:
                return reader.tracks.duration_for_video(video)
        return PlaybackCommands(self.bridge, getattr(store, 'accounts', None), getattr(store, 'tracks', None),
            getattr(store, 'playlists', None), getattr(store, 'playlist_recipes', None),
            getattr(store, 'collection', None), radio=getattr(self, 'radio', None), work=activity_for(self),
            now=getattr(self, 'now_fn', time.time), ops=lambda: self.ops(),
            rating=lambda video: ratings.main_rating(store, video),
            identity=lambda brand: live_plays.resolve_identity(store, brand), duration=duration,
            record_next=lambda *args, **kwargs: playback_feedback.record_next(store, *args, **kwargs),
            paused=lambda: bool(getattr(store, 'activity_paused', False)),
            measure=lambda *args, **kwargs: measurement.record_action(store, *args, **kwargs),
            logger=getattr(self, 'logger', logger), monotonic=monotonic)

    def bridge_connection(self):
        from contextlib import nullcontext
        from yt_playlist.controllers.bridge_connection import BridgeConnection
        store = getattr(self, 'store', None)
        return BridgeConnection(store.settings if store is not None else None,
            guard=store._lock if store is not None else nullcontext(),
            paused=lambda: bool(getattr(store, 'activity_paused', False)),
            clear_authentication=getattr(self, 'clear_all_auth_expired', lambda: None),
            wake=getattr(getattr(self, 'sync_wake', None), 'set', lambda: None))

    def bridge_observations(self, *, connected=None):
        from yt_playlist.controllers.bridge_observations import BridgeObservations
        from yt_playlist.core.activity import activity_for
        from yt_playlist.library import live_plays, player_events
        def observe(kind, body, at):
            handler = live_plays.handle_play_event if kind == 'live' else player_events.handle_player_event
            return handler(self, body, at)
        radio = getattr(self, 'radio', None)
        suspend = getattr(radio, 'suspend', None) or getattr(radio, 'stop', lambda: None)
        def prepare():
            first_sounds = getattr(self, 'first_sounds', None)
            if first_sounds is not None:
                first_sounds.prepare()
            getattr(radio, 'prepare', lambda: None)()
        return BridgeObservations(activity_for(self), observe=observe,
            connected=connected if connected is not None else Ctx.bridge_connection(self).record,
            prepare=prepare, logger=getattr(self, 'logger', logger), suspend=suspend)
