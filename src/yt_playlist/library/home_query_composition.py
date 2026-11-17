"""Open one committed Home input session, then return a detached document."""
from contextlib import ExitStack

from yt_playlist.controllers.home_query import HomeQuery
from yt_playlist.controllers.home_navigation import HomeNavigation
from yt_playlist.core import updatecheck
from yt_playlist.library.home_album_composition import album_menu
from yt_playlist.library.home_board_composition import board
from yt_playlist.library.story_composition import stories
from yt_playlist.rec import account_growth, actions, artist_start, choice_evidence, home_cache, home_preferences, home_prepared, mix_tuning, onboarding, tv
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.web.spotify_import import load_job


def navigation(ctx, reader=None):
    store = reader if reader is not None else ctx.store
    schedule = ctx.rec_worker.trigger_story_refresh if ctx.rec_worker else lambda: None
    prepared_stories = stories(store)
    return HomeNavigation(store.settings, store.rec_proposals, store.rec_ledger,
        retained=lambda panel: account_growth.retained_access(store, panel),
        stories=lambda now, epoch: prepared_stories.load(now, epoch=epoch, schedule=schedule),
        group_stories=prepared_stories.groups,
        epoch=lambda: _epoch(store, 'into_recently'),
        respond=lambda *args, **kw: choice_evidence.safe_respond(ctx.store, *args, **kw),
        refresh=lambda: ctx.rec_worker.trigger_home_refresh() if ctx.rec_worker else None)


def open_home(ctx, now, *, jobs, schedule, timing, **options):
    store = ctx.store
    paused = store._db_path is not None and (store.activity_paused or getattr(store, '_evidence_maintenance', 0)
                                            or ctx.storage_maintenance.phase == 'schema')
    if not paused:
        with timing.measure('visit_accounting'):
            home_prepared.record_visit(store, now)
        with timing.measure('tuning_expiry'):
            mix_tuning.expire(store, now)
    with ExitStack() as session:
        with timing.measure('read_admission'):
            reader = session.enter_context(store.read_view(tracked=not paused))
        with home_cache.verified_reads(reader):
            writer = reader if paused else store
            commands, _ = board(reader, writer, jobs=jobs, schedule=schedule, vibe_requests=ctx.vibe_requests,
                                 worker=ctx.rec_worker, logger=ctx.logger, display_only=True)
            albums = album_menu(reader, writer, now_playing=lambda: ctx.bridge.now_playing or {},
                                schedule=schedule, worker_available=ctx.rec_worker is not None, logger=ctx.logger)
            stories = navigation(ctx, reader).story_query
            query = HomeQuery(reader.settings, reader.accounts, reader.rec_model, commands,
                artists_required=lambda: artist_start.required(reader), setup_completed=lambda: onboarding.setup_completed(reader),
                update=lambda: updatecheck.update_nudge(reader), recap=lambda now: home_prepared.recap_notice(reader, now),
                spotify=lambda: load_job(reader),
                radio=lambda launch: home_prepared.with_launch_playlist(reader, ctx.radio.bootstrap(reader=reader), launch),
                radio_tracks=lambda state: ctx.radio.history_tracks(state, reader=reader),
                collections=lambda payload, now: home_prepared.collections(reader, payload, now),
                album_ready=lambda now: onboarding.prepared_albums_ready(reader, now),
                mix_ready=lambda now: onboarding.prepared_mixes_ready(reader, now),
                growth=lambda now, **kw: account_growth.view(reader, now, **kw), albums=albums.query, stories=stories,
                default_panel=lambda growth: home_preferences.resolve_default_panel(reader, growth),
                tv_minutes=lambda: tv.max_minutes(reader),
                actions=lambda now: actions.take_action(reader, now, {} if reader.activity_paused else ctx.auth_expired),
                sync=lambda now: actions.sync_status(reader, now),
                writable=lambda: not getattr(writer, '_read_only', False) and not writer.activity_paused)
            return query.page(now, **options)
