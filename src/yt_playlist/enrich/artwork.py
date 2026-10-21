"""Bounded optional artwork work; does not reopen or repeat a recording's metadata pass."""
from yt_playlist.providers import coverart
from yt_playlist.providers.waterfall import needs_art


def retry_due(store, now_fn, *, should_stop=lambda: False, on_progress=lambda event: None):
    completed = 0
    for job in store.enrichment.due_artwork_retries(now_fn(), limit=1):
        if should_stop():
            break
        track = store.catalog_subject(job["entry_id"])
        if not track:
            continue
        if needs_art(track):
            outcome = coverart.lookup(job["release_ids"])
            if outcome.url:
                store.set_track_art(track["id"], outcome.url)
        else:
            outcome = coverart.CoverResult("found", track.get("thumbnail"))
        store.enrichment.record_artwork_result(track["id"], job["release_ids"], outcome, now_fn())
        if outcome.status == "retry":
            on_progress({"type":"info", "warning":True,
                         "text":f"Artwork retry for {track['title']} failed: {outcome.error}"})
        else:
            completed += 1
    return completed
