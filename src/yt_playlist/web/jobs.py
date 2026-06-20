"""In-memory registry of background sync jobs, streamed to the browser over SSE."""
import itertools
import threading

class SyncJob:
    def __init__(self, job_id):
        self.id = job_id
        self.events = []      # list of event dicts; append-only (safe for one producer thread)
        self.done = False
        self.error = None

class SyncJobs:
    def __init__(self):
        self._jobs = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def create(self) -> SyncJob:
        with self._lock:
            job = SyncJob(next(self._counter))
            self._jobs[job.id] = job
            return job

    def get(self, job_id) -> "SyncJob | None":
        return self._jobs.get(job_id)
