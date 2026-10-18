"""Pause new sync/import work only after existing work has finished."""


def acquire_restart_locks(ctx):
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
