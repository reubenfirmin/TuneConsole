"""Bind application settings and host lifecycle capabilities to update commands."""
from yt_playlist.controllers.updates import UpdateCommands
from yt_playlist.core import updatecheck
from yt_playlist.core.activity import activity_for
from yt_playlist.core.update_lifecycle import PendingRestart, acquire_restart_locks
from yt_playlist.integrations.base import AccessState


def checks(store):
    return updatecheck.checks(store.settings, AccessState.for_store(store))


def desktop(ctx, stop_server):
    ctx.pending_restart = pending = PendingRestart(
        activity_for(ctx), ctx.updater, (ctx.sync_lock, ctx.spotify_import_lock),
        ctx.jobs, ctx.logger, stop_server)
    return UpdateCommands(ctx.updater, pending)


def acquire_shutdown(ctx, **options):
    return acquire_restart_locks(activity_for(ctx), (ctx.sync_lock, ctx.spotify_import_lock),
                                 ctx.jobs, getattr(ctx, 'updater', None), **options)
