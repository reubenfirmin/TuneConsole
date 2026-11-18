"""Synchronization command wiring shared by job dispatch and recurring sync."""
from yt_playlist.controllers.sync_execution import SyncExecution
from yt_playlist.controllers.sync_commands import SyncCommands
from yt_playlist.core.activity import activity_for
from yt_playlist.library import sync_outcomes, ratings
from yt_playlist.library.playlist_positions import retry_insertions
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.controllers.library_synchronization import LibrarySynchronization


def synchronization(store):
    return LibrarySynchronization(accounts=store.accounts, collection=store.collection,
        history=store.history, playlists=store.playlists, settings=store.settings, tracks=store.tracks,
        sync=store.sync, actions=store.actions, reconcile_ratings=lambda *args: ratings.reconcile_pending(store, *args),
        record_ratings=lambda *args, **kwargs: ratings.record_observations(store, *args, **kwargs),
        retry_insertions=lambda *args, **kwargs: retry_insertions(store, *args, **kwargs),
        remember_album=lambda *args: remember_album(store, *args))


def execution(ctx):
    def wake():
        for worker in (ctx.rec_worker, ctx.enrich_worker) if activity_for(ctx).accepting else ():
            if worker:
                try:
                    worker.trigger()
                except Exception:
                    ctx.logger.warning('post-sync worker trigger failed', exc_info=True)

    return SyncExecution(begin=lambda now: sync_outcomes.begin_attempt(ctx.store, now),
        synchronize=lambda clients, now, **options: synchronization(ctx.store).run(clients, now,
            on_auth_expired=ctx.flag_auth_expired, on_auth_ok=ctx.clear_auth_expired, **options),
        clients=ctx.client_provider, accounts=ctx.store.accounts.get_accounts,
        finish=lambda result, now: sync_outcomes.finish_attempt(ctx.store, result, now),
        now=lambda: ctx.now_fn(), wake=wake)


def commands(ctx):
    if not hasattr(ctx, '_sync_commands'):
        def state():
            with ctx.store.read_view() as reader:
                return reader.sync.state()
        ctx._sync_commands = SyncCommands(ctx.jobs, ctx.sync_lock, activity_for(ctx),
            lambda: execution(ctx), state=state,
            due=lambda max_age_s, **options: sync_outcomes.sync_due(
                ctx.store, ctx.now_fn(), max_age_s, **options),
            enabled=lambda: not getattr(ctx.store, '_recommendation_retirement_pending', False))
    return ctx._sync_commands
