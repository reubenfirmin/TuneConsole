"""Compose Home and its display lookups through one committed read session."""
from contextlib import ExitStack, contextmanager
from functools import cache

from yt_playlist.controllers.home_query import HomeQuery, PANELS
from yt_playlist.controllers.home_navigation import HomeNavigation
from yt_playlist.core import updatecheck
from yt_playlist.library.home_album_composition import album_menu
from yt_playlist.library.home_board_composition import board
from yt_playlist.library.story_composition import stories
from yt_playlist.rec import account_growth, actions, artist_start, choice_evidence, growth_access, home_cache, home_preferences, home_prepared, mix_tuning, onboarding, tv
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


def action_notices(ctx, reader, now):
    return actions.take_action(reader, now, {} if reader.activity_paused else ctx.auth_expired)


def panel_access(ctx, panel):
    with ctx.store.read_view(tracked=True) as reader:
        return account_growth.opening_access(reader, ctx.now_fn(), panel, account_growth.retained_usage(reader),
                                             lambda: ctx.radio.bootstrap(reader=reader))


@contextmanager
def open_home(ctx, now, *, jobs, schedule, timing, defer, **options):
    store = ctx.store
    paused = store._db_path is not None and (store.activity_paused or getattr(store, '_evidence_maintenance', 0))
    with ExitStack() as session:
        with timing.measure('read_admission'):
            reader = session.enter_context(store.read_view(tracked=not paused))
        with home_cache.verified_reads(reader):
            writer = reader if paused else store
            commands, _ = board(reader, writer, jobs=jobs, worker=ctx.rec_worker, logger=ctx.logger, defer=defer)
            albums = album_menu(reader, writer, now_playing=lambda: ctx.bridge.now_playing or {},
                                schedule=schedule, worker_available=ctx.rec_worker is not None, logger=ctx.logger)
            stories = navigation(ctx, reader).story_query
            @cache
            def usage():
                return account_growth.retained_usage(reader)

            @cache
            def radio():
                return ctx.radio.bootstrap(reader=reader)

            visited_cards = ()

            def visit(panel):
                nonlocal visited_cards
                if paused:
                    return
                visited_cards = {'directions': ('wheelhouse', 'explore', 'comfort', 'fresh', 'cards'),
                         'discover': ('discover', 'new_artists'), 'rediscover': ('rediscover',),
                         'recent': ('into_recently',)}.get(panel, ())
                if panel == 'directions':
                    with timing.measure('tuning_expiry'):
                        mix_tuning.expire(store, now, reader=reader)

            query = HomeQuery(reader.settings, reader.accounts, reader.rec_model, commands,
                artists_required=lambda: artist_start.required(reader), setup_completed=lambda: onboarding.setup_completed(reader),
                update=lambda: updatecheck.update_nudge(reader), recap=lambda now: home_prepared.recap_notice(reader, now),
                spotify=lambda *, status_only=False: (reader.settings.spotify_import_status()
                                                       if status_only else load_job(reader)),
                radio=lambda launch: home_prepared.with_launch_playlist(reader, radio(), launch),
                radio_tracks=lambda state: ctx.radio.history_tracks(state, reader=reader),
                revisit=lambda now: home_prepared.revisit(reader, now),
                access=lambda panel: account_growth.opening_access(reader, now, panel, usage(), radio),
                earned=lambda: [panel for panel in PANELS if growth_access.retained_access(usage(), panel)],
                visit=visit,
                growth=lambda now, **kw: account_growth.view(reader, now, defer_vibes=True, **kw),
                albums=albums.query, stories=stories,
                preferred=lambda: home_preferences.PANELS[home_preferences.default_format(reader)],
                tv_minutes=lambda: tv.max_minutes(reader),
                actions=lambda now: action_notices(ctx, reader, now),
                sync=lambda now: actions.sync_status(reader, now),
                writable=lambda: not getattr(writer, '_read_only', False) and not writer.activity_paused)
            model = query.page(now, **options)
            if visited_cards:
                # Persist the displayed menu before this visit can advance the rotation.
                defer('visit accounting', home_prepared.record_visit, store, now, visited_cards)
        yield model, reader
