"""Bounded collection of disposable superseded predictions, never originals."""
RETAIN_SECONDS = 180 * 86400


def maintain(store, now, limit=128):
    return store.vibe_predictions.maintain_history(now, limit)
