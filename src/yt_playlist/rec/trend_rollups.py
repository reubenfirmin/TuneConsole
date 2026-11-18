"""Background listening-rollup calculation and ordered publication.

Calculations use detached rows from a private read snapshot. Monthly statistics and recap
assembly have separate owners; only this pipeline publishes their result.
"""
from yt_playlist.library import trend_composition
from yt_playlist.controllers.trend_preparation import day_counts


def mark_viewed(store, month) -> None:
    """Record that the recap for `month` has been viewed, so Home's nag card (home_prepared.py)
    stops surfacing it."""
    store.settings.set_setting("recap_viewed_month", month)


def day_counts_source(store) -> list:
    """Prefer exact plays with GENERATED_GROUP quarantine; otherwise distinct account listen days.

    Coarse capture dates retain unknown precision and cannot establish repeat counts when an exact
    ledger exists. Earlier coarse evidence still participates in the repository's first-play index.
    """
    return day_counts(store.trends)


def build(store, now) -> dict | None:
    """Calculate privately and publish only if the captured source generation still matches.

    Return None when source changes require another worker pass. The previously published rollup
    and first-play index remain intact; stale work cannot undo source removal or a reset.
    """
    return trend_composition.preparation(store).build(now)


def _calculate(store, now) -> dict:
    """Read monthly inputs from the privately prepared snapshot and assemble the recap."""
    return trend_composition.calculate(store, now)
