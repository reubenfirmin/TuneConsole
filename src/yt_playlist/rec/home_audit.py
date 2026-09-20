"""Diagnostic history of Home offers. Nothing in this file feeds model selection or training."""
import json
import logging
from datetime import datetime, timezone

from yt_playlist.rec.ordering import _field
from yt_playlist.rec.recipes import theme_counts

logger = logging.getLogger("yt_playlist.home_audit")
logger.propagate = False


def record(store, protos, now, *, source, epoch, card_epochs):
    """One JSON line per rendered response, including repeated previews and empty rows.
    This records server offers, not proof that a browser displayed or a person noticed them.
    Metadata is snapshotted now so later enrichment cannot rewrite the history."""
    cards = []
    for position, proto in enumerate(protos, 1):
        tracks = proto["tracks"]
        genres, decades = theme_counts(store, tracks)
        cards.append({
            "position": position, "framing": proto["lane"], "label": proto["label"],
            "description": proto["note"], "mode_id": proto.get("mode_id"),
            "epoch": epoch if proto.get("mode_id") is not None else card_epochs[proto["lane"]],
            "recipe": proto.get("recipe"), "track_count": len(tracks),
            "genres": dict(genres), "decades": dict(decades),
            "unknown_genre_count": len(tracks) - sum(genres.values()),
            "unknown_decade_count": len(tracks) - sum(decades.values()),
            "track_keys": [_field(t, "key") for t in tracks],
        })
    logger.info("%s", json.dumps({
        "schema": 1, "event": "home_cards_offered",
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "source": source, "menu_epoch": epoch, "cards": cards,
    }, ensure_ascii=False))
