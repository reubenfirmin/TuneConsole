"""Close admission before checking whether existing work has finished."""
from yt_playlist.core.activity import activity_for


def acquire_restart_locks(ctx, *, reason="restart", wait_for_update=True):
    work = activity_for(ctx)
    work.close_admission(reason)
    updater = getattr(ctx, "updater", None)
    if wait_for_update and updater is not None and updater.busy:
        return None, "Waiting for the update to finish…"
    if work.active:
        return None, "Waiting for background work to finish…"
    acquired = []
    for lock in (ctx.sync_lock, ctx.spotify_import_lock):
        if not lock.acquire(blocking=False):
            for held in acquired:
                held.release()
            return None, "Waiting for your sync or import to finish…"
        acquired.append(lock)
    if ctx.jobs.has_active():
        for held in acquired:
            held.release()
        return None, "Waiting for background work to finish…"
    return acquired, ""
