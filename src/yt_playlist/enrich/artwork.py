"""Bounded optional artwork work; does not reopen or repeat a recording's metadata pass."""
from yt_playlist.providers import coverart
from yt_playlist.controllers.artwork import Artwork


def retry_due(store, now_fn, *, should_stop=lambda: False, on_progress=lambda event: None):
    def lookup(release_ids):
        with coverart.access(store):
            return coverart.lookup(release_ids)

    return Artwork(store.enrichment_state, store.tracks, lookup, coverart.CoverResult,
                   now=now_fn).retry_due(should_stop=should_stop, on_progress=on_progress)
