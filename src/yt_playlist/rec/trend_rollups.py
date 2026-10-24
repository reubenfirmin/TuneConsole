"""Background listening-rollup pipeline and atomic publication.

Calculations use detached rows from a private read snapshot. Pure statistics, editorial insight
policy and display assembly have separate owners; only this pipeline publishes their result.
"""
import json

from yt_playlist.rec import trend_insights, trend_payload, trend_statistics as stats


def day_counts_source(store) -> list:
    """Prefer exact plays with GENERATED_GROUP quarantine; otherwise distinct account listen days.

    Coarse capture dates retain unknown precision and cannot establish repeat counts when an exact
    ledger exists. Earlier coarse evidence still participates in the repository's first-play index.
    """
    if store.trends.has_play_ledger():
        return store.trends.ledger_day_counts()
    return store.trends.play_day_counts()


def repetition_series(store) -> dict:
    """Exact, eligible evidence for the detector's shrinking return gaps."""
    return store.trends.repetition_series(trend_insights.REPETITION_MIN_RETURNS + 1)


def build(store, now) -> dict | None:
    """Calculate privately and publish only if the captured source generation still matches.

    Return None when source changes require another worker pass. The previously published rollup
    and first-play index remain intact; stale work cannot undo source removal or a reset.
    """
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        first_play = reader.trends.prepare_first_play()
        imported = reader.settings.get_setting("takeout_imported_at")
        payload = _calculate(reader, now)
        review, spotlight = payload["review"], payload["spotlight"]
        # Only consume a review when it won the spotlight cascade.
        review_month = (review["month"] if review is not None and spotlight is not None
                        and spotlight["signature"] == f"month_review:{review['month']}" else None)
        encoded = json.dumps(payload)
        if not snapshot.publish(lambda: store.trends.publish_rollup(
                first_play, encoded, now, imported, review_month)):
            return None
        return payload


def _calculate(store, now) -> dict:
    """Read inputs from the privately prepared snapshot, then run the three calculation owners."""
    day_counts = day_counts_source(store)
    meta = store.trends.track_meta()
    artist_first = store.trends.first_play_map("artist")
    track_first = store.trends.first_play_map("track")
    catalog_artists = store.trends.catalog_artists()
    catalog_keys = store.trends.catalog_track_keys()
    weeks = stats.compute_weeks(day_counts, meta, artist_first, track_first, catalog_artists, catalog_keys)
    months = stats.compute_months(day_counts, meta, artist_first, catalog_artists)
    floor_day = store.trends.first_play_floor_day()
    insights = trend_insights.detect_insights(
        day_counts, meta, artist_first, floor_day, repetition_series(store), now)
    return trend_payload.assemble(store, now, day_counts, meta, weeks, months, insights, floor_day)
