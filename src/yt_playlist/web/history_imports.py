"""Compatibility wiring for the transport-neutral history import owner."""
import threading
from contextlib import nullcontext

from yt_playlist.controllers.history_imports import HistoryImports as ImportCommands
from yt_playlist.core.activity import activity_for
from yt_playlist.library import spotify_data, takeout
from yt_playlist.web.jobs import SyncJobs


def HistoryImports(ctx):
    if not hasattr(ctx, 'jobs'):
        ctx.jobs = SyncJobs()
    if not hasattr(ctx, 'sync_lock'):
        ctx.sync_lock = threading.Lock()
    store = ctx.store
    return ImportCommands(jobs=ctx.jobs, lock=ctx.sync_lock,
        model_lock=lambda: getattr(ctx.rec_worker, 'model_lock', nullcontext()),
        work=activity_for(ctx), settings=store.settings, history=store.history,
        importers={'takeout': lambda raw, **kw: takeout.import_takeout(store, raw, **kw),
                   'spotify': lambda raw, **kw: spotify_data.import_spotify_data(store, raw, **kw)},
        seed_spotify=store.discovery.seed_spotify_artists,
        seed_takeout=lambda *args, **kw: takeout.seed_discovery_from_unmatched(store, *args, **kw),
        resolve_spotify=lambda: spotify_data.resolve_import_identity(store),
        refresh=lambda: ctx.rec_worker.trigger() if ctx.rec_worker else None,
        now=lambda: ctx.now_fn())
