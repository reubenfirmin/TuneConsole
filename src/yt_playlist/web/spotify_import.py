"""Persisted UI state for the background Spotify library import."""
from __future__ import annotations

import json
import math

from yt_playlist.util.matching import identity_key


JOB_KEY = "spotify_library_import_job"
ACTIVE_STATUSES = {"queued", "running"}


def load_job(store) -> dict | None:
    try:
        value = json.loads(store.get_setting(JOB_KEY) or "null")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def save_job(store, job: dict) -> None:
    store.set_setting(JOB_KEY, json.dumps(job, sort_keys=True))


def current_job(ctx) -> dict | None:
    """Return current state, turning a pre-restart running marker into a retryable failure."""
    job = load_job(ctx.store)
    if (job and job.get("status") in ACTIVE_STATUSES
            and not ctx.spotify_import_lock.locked()):
        job = {**job, "status": "error", "finished_at": ctx.now_fn(),
               "error": "TuneConsole stopped before the import finished. Partial work was saved, "
                        "so it is safe to retry the same export."}
        save_job(ctx.store, job)
    return job


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
