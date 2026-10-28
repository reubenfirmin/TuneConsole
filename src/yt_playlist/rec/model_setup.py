"""Shared model configuration and fitting, independent of comparison orchestration."""
from yt_playlist.rec import embedding_training as training

MIN_HISTORY_DAYS = 14
MIN_TRACKS = 100
CONFIGS = [("svd", 48), ("svd", 64), ("svd", 96), ("svd", 128), ("item2vec", 64)]


def current_setup(store):
    return {"method": store.settings.get_setting("rec_embed_method") or "svd",
            "dim": int(store.settings.get_setting("rec_dim") or training.DIM)}


def build(store, setup):
    return training.build_vectors_from_baskets(
        store.rec_evidence.rec_baskets(), dim=setup["dim"], method=setup["method"],
        weights=training.cooc_weights(store.rec_query.play_counts())
        if store.settings.get_setting("rec_cooc_weighting") == "1" else None)
