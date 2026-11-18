"""Bind feature commands to the one existing recommendation worker."""
from yt_playlist.controllers.recommendation_jobs import (CollectionCommands, GroupCommands,
    MaintenanceCommands, ModelCommands, PendingCommands)


def pending_commands(worker):
    return PendingCommands(worker.ctx.store, worker.ctx.now_fn, worker._stage, worker.tasks,
        retention=worker.retention.run, prepare=worker._prepare_content,
        full_due=lambda: worker.preparation.full_due(worker.ctx.now_fn()),
        rebuild=worker._do_rebuild, modes=worker._do_mode_refresh, bundles=worker._refresh_bundles,
        projection=lambda name: worker._refresh_trends() if name == 'trends' else worker.projections.run(name),
        poll_tv=worker._poll_tv, groups=worker._prepare_groups)


def model_commands(worker):
    return ModelCommands(worker.ctx.store, worker.ctx.now_fn, worker._stage, worker.preparation,
                         worker._fit_cache, worker._stop.is_set, worker.ctx.logger)


def group_commands(worker):
    return GroupCommands(worker.ctx.store, worker.ctx.now_fn, worker._stage, worker.preparation.groups,
                         worker._stop.is_set, worker.ctx.logger)


def maintenance_commands(worker):
    return MaintenanceCommands(worker.ctx.store, worker.ctx.now_fn, worker.preparation)


def collection_commands(worker):
    from yt_playlist.rec import discover
    return CollectionCommands(worker.ctx.store, worker.ctx.client_provider, worker.ctx.now_fn,
        worker.work.wrap, worker.ctx.logger, lambda now: discover.gc_discovery(worker.ctx, now))
