"""Compatibility entry points; recap calculations live below acquisition."""
from yt_playlist.library import trend_composition


def month_review(months, store, now, day_counts=(), meta=None):
    return trend_composition.review(store, months, now, day_counts, meta)


def assemble(store, now, day_counts, meta, months):
    return trend_composition.assemble(store, now, day_counts, meta, months)
