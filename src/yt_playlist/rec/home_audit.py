"""Diagnostic history of Home offers. Nothing in this file feeds model selection or training."""
import json
import logging
from datetime import datetime, timezone

from yt_playlist.rec.ordering import _field
from yt_playlist.rec.recipes import theme_count_values

logger = logging.getLogger("yt_playlist.home_audit")
logger.propagate = False


def _catalog(store, protos):
    """Acquisition: one batched genre/decade fallback for every card's tracks, so the per-card
    loop below never queries the store again (theme_counts otherwise re-queries per card)."""
    tracks = [t for proto in protos for t in proto["tracks"]]
    return {
        'artists': store.rec_query.artist_genre_years_for([_field(t, 'artist') or '' for t in tracks]),
        'decades': store.rec_query.track_decades([_field(t, 'key') or '' for t in tracks]),
    }


def record(store, protos, now, *, source, epoch, card_epochs):
    """One JSON line per rendered response, including repeated previews and empty rows.
    This records server offers, not proof that a browser displayed or a person noticed them.
    Metadata is snapshotted now so later enrichment cannot rewrite the history."""
    catalog = _catalog(store, protos)
    logger.info('%s', json.dumps(projection(protos, catalog, now, source=source,
        epoch=epoch, card_epochs=card_epochs), ensure_ascii=False))


def projection(protos, catalog, now, *, source, epoch, card_epochs):
    cards = []
    for position, proto in enumerate(protos, 1):
        tracks = proto["tracks"]
        genres, decades = theme_count_values(tracks, catalog)
        cards.append({
            "position": position, "framing": proto["lane"], "label": proto["label"],
            "description": proto["note"], "mode_id": proto.get("mode_id"),
            "epoch": epoch if proto.get("mode_id") is not None else card_epochs[proto["lane"]],
            "track_count": len(tracks),
            "genres": dict(genres), "decades": dict(decades),
            "unknown_genre_count": len(tracks) - sum(genres.values()),
            "unknown_decade_count": len(tracks) - sum(decades.values()),
        })
    return {
        "schema": 1, "event": "home_cards_offered",
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "source": source, "menu_epoch": epoch, "cards": cards,
    }
