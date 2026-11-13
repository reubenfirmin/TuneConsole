"""Background listening-rollup calculation and ordered publication.

Calculations use detached rows from a private read snapshot. Monthly statistics and recap
assembly have separate owners; only this pipeline publishes their result.
"""
import json
from yt_playlist.core.db_revisions import column_group_reads

from yt_playlist.rec import trend_payload, trend_statistics as stats


def mark_viewed(store, month) -> None:
    """Record that the recap for `month` has been viewed, so Home's nag card (home_prepared.py)
    stops surfacing it."""
    store.settings.set_setting("recap_viewed_month", month)


def day_counts_source(store) -> list:
    """Prefer exact plays with GENERATED_GROUP quarantine; otherwise distinct account listen days.

    Coarse capture dates retain unknown precision and cannot establish repeat counts when an exact
    ledger exists. Earlier coarse evidence still participates in the repository's first-play index.
    """
    if store.trends.has_play_ledger():
        return store.trends.ledger_day_counts()
    return store.trends.play_day_counts()


def build(store, now) -> dict | None:
    """Calculate privately and publish only if the captured source generation still matches.

    Return None when source changes require another worker pass. The previously published rollup
    and first-play index remain intact; stale work cannot undo source removal or a reset.
    """
    with store.read_snapshot() as snapshot, column_group_reads(snapshot.store, 'recordings', 'catalog'):
        reader = snapshot.store
        first_play = reader.trends.prepare_first_play()
        imported = reader.settings.get_setting("takeout_imported_at")
        payload = _calculate(reader, now)
        encoded = json.dumps(payload)
        if not snapshot.is_current():
            return None
        snapshot.release_reader()
        store.trends.publish_rollup(first_play, encoded, now, imported)
        return payload


def _calculate(store, now) -> dict:
    """Read monthly inputs from the privately prepared snapshot and assemble the recap."""
    day_counts = day_counts_source(store)
    meta = store.trends.track_meta()
    artist_first = store.trends.first_play_map("artist")
    catalog_artists = store.trends.catalog_artists()
    genre_axes = store.genres.feature_axes(row[1] for row in meta.values())
    months = stats.compute_months(day_counts, meta, artist_first, catalog_artists,genre_axes=genre_axes)
    return trend_payload.assemble(store, now, day_counts, meta, months)
