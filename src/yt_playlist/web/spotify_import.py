"""Persisted UI state for the background Spotify library import."""
from __future__ import annotations

import math
import secrets
import threading

from yt_playlist.util.matching import identity_key


JOB_KEY = "spotify_library_import_job"
ACTIVE_STATUSES = {"queued", "running"}


class TransferPreviews:
    """Bounded, expiring, single-use source snapshots; never written to the store."""

    def __init__(self, ttl=7200, capacity=8):
        self.ttl, self.capacity = ttl, capacity
        self._entries = {}
        self._lock = threading.Lock()

    def _expire(self, now):
        for token, (deadline, _) in list(self._entries.items()):
            if deadline <= now:
                del self._entries[token]

    def put(self, value, now):
        with self._lock:
            self._expire(now)
            while len(self._entries) >= self.capacity:
                del self._entries[next(iter(self._entries))]
            token = secrets.token_urlsafe(32)
            self._entries[token] = (now + self.ttl, value)
            return token

    def get(self, token, now, *, consume=False):
        with self._lock:
            self._expire(now)
            entry = self._entries.pop(token, None) if consume else self._entries.get(token)
            return entry[1] if entry else None


def load_job(store) -> dict | None:
    try:
        value = store.settings.spotify_import_job()
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _retained_report(job: dict) -> dict:
    """Trim a schema-5+ report before it becomes the lasting audit: playlist names/counts and
    YouTube destinations stay; source song metadata (including unmatched/ambiguous rows) is
    available only in the active preview, never persisted."""
    if job.get("report_schema", 0) >= 5 and job.get("report"):
        report = job["report"]
        details = {**report["details"], "unmatched_tracks": []}
        return {**job, "report": {**report, "details": details}}
    return job


def save_job(store, job: dict) -> None:
    original = job
    job = _retained_report(job)
    revision = store.settings.save_spotify_import_job(job)
    if revision is not None:
        from yt_playlist.core.spotify_job_state import TOKEN
        original[TOKEN] = revision


def _recovered(job: dict, now) -> dict:
    """Pure: project a pre-restart running/queued job as a retryable failure."""
    return {**job, "status": "error", "finished_at": now,
            "error": "TuneConsole stopped before the import finished. Upload the same export "
                     "again to resume recorded transfers."}


def current_job(ctx) -> dict | None:
    """Return current state, turning a pre-restart running marker into a retryable failure."""
    job = load_job(ctx.store)
    if not job or job.get("status") not in ACTIVE_STATUSES:
        return job
    if not ctx.spotify_import_lock.acquire(blocking=False):
        return job
    try:
        # The worker may have finished, or another operation may have replaced/deleted
        # the report, since the first read. Recovery owns and checks the latest state.
        job = load_job(ctx.store)
        if job and job.get("status") in ACTIVE_STATUSES:
            job = _recovered(job, ctx.now_fn())
            # Return the recovery projection during maintenance; persist it on
            # the next ordinary read. Recheck under the lock so a cutover cannot
            # start between the pause check and the settings write.
            while not ctx.store.activity_paused:
                if ctx.store._lock.acquire(timeout=.025):
                    try:
                        if not ctx.store.activity_paused:
                            save_job(ctx.store, job)
                    finally:
                        ctx.store._lock.release()
                    break
        return job
    finally:
        ctx.spotify_import_lock.release()


def estimate(parsed: dict) -> dict:
    """Build a deliberately conservative duration estimate from the parsed export."""
    tracks = [track for playlist in parsed["playlists"] for track in playlist["tracks"]]
    distinct_tracks = len({identity_key(track["title"], track["artist"]) for track in tracks})
    lookups = distinct_tracks + len(parsed["albums"])
    # A bridge lookup is usually much faster than this, but throttling and retries make large imports
    # bursty. 750/hour gives the UI an honest upper-budget instead of an optimistic stopwatch.
    minutes = max(5, math.ceil(lookups / 12.5))
    if minutes < 60:
        label = f"about {int(math.ceil(minutes / 5) * 5)} minutes"
    else:
        hours = math.ceil(minutes / 60)
        label = f"up to about {hours} hour{'s' if hours != 1 else ''}"
    return {
        "playlists_found": len(parsed["playlists"]),
        "track_entries": len(tracks),
        "albums_found": len(parsed["albums"]),
        "lookup_count": lookups,
        "estimate_label": label,
    }
