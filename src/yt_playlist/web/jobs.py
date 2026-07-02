"""In-memory registry of background sync jobs, streamed to the browser over SSE."""
import itertools
import threading

class SyncJob:
    def __init__(self, job_id, kind=None):
        self.id = job_id
        self.kind = kind      # "library" for a full/plays library sync; None for enrichment jobs
        self.events = []      # list of event dicts; append-only (safe for one producer thread)
        self.done = False
        self.error = None
        self.playlist_id = None   # set for playlist enrichment jobs, so a refreshed page can rejoin
        self.album_browse = None  # set for album enrichment jobs (browse_id scope) instead
        self.source = None        # enrichment source (musicbrainz / lastfm / discogs)

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
