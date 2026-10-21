"""In-memory registry of background sync jobs, streamed to the browser over SSE."""
import itertools
import logging
import threading

from yt_playlist.providers import enrichment, waterfall
from yt_playlist.core.activity import activity_for


def run_library_sync(ctx, job):
    """Run with the caller's sync lock held; publish terminal state only after error handling."""
    from yt_playlist.library import sync
    from yt_playlist.library.sync_outcomes import begin_attempt, finish_attempt, resource
    from yt_playlist.util.retry import error_summary

    now = ctx.now_fn()
    try:
        begin_attempt(ctx.store, now)
        clients = ctx.client_provider() or {}
        job.result = sync.sync_all(ctx.store, clients, now, on_progress=job.events.append,
                                   on_auth_expired=ctx.flag_auth_expired,
                                   on_auth_ok=ctx.clear_auth_expired)
        job.outcome = job.result["status"]
        if job.outcome != "complete":
            job.error = f"Sync {job.outcome}; some library resources could not be refreshed."
    except Exception as e:  # the provider and unexpected failures follow the same SSE contract
        detail = error_summary(e)
        job.error = detail
        job.outcome = "failed"
        job.events.append({"type": "err", "text": f"sync failed: {detail}"})
        accounts = {str(i.id): {"label": i.label, "status": "failed", "resources": {
            name: resource("failed", detail)
            for name in ("playlists", "ratings", "history", "saved_albums")}}
            for i in ctx.store.identities.get_identities()}
        job.result = finish_attempt(ctx.store, {"status": "failed", "accounts": accounts,
            "resources": {name: resource("failed", detail)
                          for name in ("saved_album_mirror", "album_tracks")}}, now)
    finally:
        for worker in (ctx.rec_worker, ctx.enrich_worker):
            if worker:
                try:
                    worker.trigger()
                except Exception:  # rebuilding must not hide the sync's terminal result
                    logging.getLogger(__name__).warning("post-sync worker trigger failed", exc_info=True)
        job.done = True


def start_playlist_enrichment(ctx, playlist_id):
    """Start or rejoin a playlist waterfall, shared by creation and the manual enrich action."""
    active = ctx.jobs.find_active(playlist_id)
    if active is not None:
        return active
    job = ctx.jobs.create()
    job.playlist_id = playlist_id
    job.source = "waterfall"

    def run():
        try:
            waterfall.run_waterfall(ctx.store, ctx.store.tracks.tracks_for_waterfall(playlist_id),
                                    enrichment.load_config(ctx.store), on_progress=job.events.append)
        except Exception as e:  # noqa: BLE001
            detail = str(e) or type(e).__name__
            job.error = detail
            job.events.append({"type": "err", "text": f"enrichment failed: {detail}"})
        finally:
            job.done = True

    activity_for(ctx).start_thread(run)
    return job


def start_fresh_pool(ctx, parameters):
    """Build a requested cold pool off the request thread; repeated clicks rejoin the same job."""
    job, created = ctx.jobs.create_or_rejoin("fresh_pool", parameters)
    if not created:
        return job

    def run():
        from yt_playlist.rec import fresh_pool
        try:
            fresh_pool.populate(ctx, **parameters,
                                on_progress=lambda text: job.events.append({"text": text}))
            job.events.append({"text": "Preparing your fresh mix…"})
            fresh_pool.prepare(ctx.store, ctx.now_fn())
        except Exception as e:  # noqa: BLE001
            ctx.logger.warning("Fresh pool build failed", exc_info=True)
            job.error = (str(e) if isinstance(e, ValueError)
                         else "Couldn't find fresh songs just now. Try again.")
        finally:
            job.done = True

    activity_for(ctx).start_thread(run)
    return job


def start_library_pool(ctx, parameters, exclude_keys):
    """Prepare an unfilled library lane without doing ranking on the page request."""
    job, created = ctx.jobs.create_or_rejoin("library_pool", parameters)
    if not created:
        return job
    job.exclude_keys = exclude_keys

    def run():
        from yt_playlist.rec import library_pool
        try:
            job.events.append({"text": "Finding matching songs in your library…"})
            library_pool.prepare(ctx.store, ctx.now_fn(), parameters)
        except Exception:  # noqa: BLE001
            ctx.logger.warning("Library pool build failed", exc_info=True)
            job.error = "Couldn't prepare this mix just now. Try again."
        finally:
            job.done = True

    activity_for(ctx).start_thread(run)
    return job


class SyncJob:
    def __init__(self, job_id, kind=None):
        self.id = job_id
        self.kind = kind      # "library" for a full/plays library sync; None for enrichment jobs
        self.events = []      # list of event dicts; append-only (safe for one producer thread)
        self.done = False
        self.error = None
        self.outcome = None
        self.result = None
        self.playlist_id = None   # set for playlist enrichment jobs, so a refreshed page can rejoin
        self.album_browse = None  # set for album enrichment jobs (browse_id scope) instead
        self.source = None        # enrichment source (musicbrainz / lastfm / discogs)
        self.parameters = None    # immutable request parameters for an on-demand pool build
        self.exclude_keys = []    # sibling mixes visible when a library pool was requested

class SyncJobs:
    def __init__(self):
        self._jobs = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def create(self, kind=None) -> SyncJob:
        with self._lock:
            job = SyncJob(next(self._counter), kind=kind)
            self._jobs[job.id] = job
            return job

    def get(self, job_id) -> "SyncJob | None":
        return self._jobs.get(job_id)

    def latest(self, kind, parameters):
        with self._lock:
            return next((job for job in reversed(list(self._jobs.values()))
                         if job.kind == kind and job.parameters == parameters), None)

    def create_or_rejoin(self, kind, parameters):
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job.kind == kind and job.parameters == parameters and not job.done:
                    return job, False
            job = SyncJob(next(self._counter), kind=kind)
            job.parameters = parameters
            self._jobs[job.id] = job
            return job, True

    def has_active(self) -> bool:
        with self._lock:
            return any(not job.done for job in self._jobs.values())

    def find_active_library(self) -> "SyncJob | None":
        """The most recent still-running library sync, so the dashboard's live console can attach to
        whatever sync is in flight (the automatic background sync as well as a manual one)."""
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job.kind == "library" and not job.done:
                    return job
        return None

    def find_active(self, playlist_id) -> "SyncJob | None":
        """The most recent still-running enrichment job for a playlist (for page-refresh rejoin)."""
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job.playlist_id == playlist_id and not job.done:
                    return job
        return None
