"""#83 one metric per sweep: a grid can never compare temporal scores against in-sample scores.

Tests via monkeypatching temporal_recall/recall_at_k at the eval_recs module level to deterministic
stubs (this is pure orchestration logic; the metrics themselves have their own suites). autotune also
calls embed.build_and_store per config, so that is stubbed too (it only needs to not crash on a tiny
in-memory store), following tests/test_autotune.py's mechanics.
"""
from yt_playlist.rec import eval_recs


def _stub_build_and_store(monkeypatch):
    monkeypatch.setattr(eval_recs.embed, "build_and_store", lambda store, dim=None: 0)


def _stub_build_and_store_tracking_dim(monkeypatch):
    """Like _stub_build_and_store, but also mirrors the built dim into the rec_dim setting (autotune
    itself only persists rec_dim on the winner, after the sweep) so a stub can key off (method, dim) for
    each build the sweep performs. Returns a dict with a "n" counter of builds so far, letting a stub
    distinguish the pre-sweep pick/previous-score calls (n == 0, no config built yet) from in-sweep ones."""
    builds = {"n": 0}

    def _build(store, dim=None):
        builds["n"] += 1
        if dim is not None:
            store.set_setting("rec_dim", str(dim))
        return 0

    monkeypatch.setattr(eval_recs.embed, "build_and_store", _build)
    return builds


def test_temporal_available_every_row_temporal_and_not_in_sample(store, monkeypatch):
    _stub_build_and_store(monkeypatch)
    monkeypatch.setattr(eval_recs, "temporal_recall", lambda store, holdout_days=30, k=20: {"recall": 0.5})
    monkeypatch.setattr(eval_recs, "recall_at_k",
                         lambda store, k=20, min_size=5, seed=0: {"recall_at_k": None})

    res = eval_recs.autotune(store)

    assert res["metric"] == "temporal_recall"
    assert res["in_sample"] is False
    for row in res["grid"]:
        assert row["metric"] == "temporal_recall"
    assert res["previous"]["metric"] == "temporal_recall"


def test_temporal_unavailable_at_pick_time_every_row_recall_at_k_in_sample(store, monkeypatch):
    _stub_build_and_store(monkeypatch)
    monkeypatch.setattr(eval_recs, "temporal_recall", lambda store, holdout_days=30, k=20: {"recall": None})
    monkeypatch.setattr(eval_recs, "recall_at_k",
                         lambda store, k=20, min_size=5, seed=0: {"recall_at_k": 0.3})

    res = eval_recs.autotune(store)

    assert res["metric"] == "recall_at_k"
    assert res["in_sample"] is True
    for row in res["grid"]:
        assert row["metric"] == "recall_at_k"
    assert res["previous"]["metric"] == "recall_at_k"


def test_temporal_none_mid_sweep_scores_zero_failed_and_keeps_metric(store, monkeypatch):
    builds = _stub_build_and_store_tracking_dim(monkeypatch)
    monkeypatch.setattr(eval_recs, "recall_at_k",
                         lambda store, k=20, min_size=5, seed=0: {"recall_at_k": None})

    # svd/48 fails temporal mid-sweep; every other config succeeds.
    scores = {"svd": {48: None, 64: 0.4, 96: 0.6, 128: 0.2}, "item2vec": {64: 0.1}}

    def _temporal(store, holdout_days=30, k=20):
        if builds["n"] == 0:
            return {"recall": 0.9}   # pre-sweep: metric pick + previous-score calls, nothing built yet
        method = store.get_setting("rec_embed_method") or "svd"
        dim = int(store.get_setting("rec_dim") or 48)
        return {"recall": scores.get(method, {}).get(dim)}

    monkeypatch.setattr(eval_recs, "temporal_recall", _temporal)

    res = eval_recs.autotune(store, svd_dims=(48, 64, 96, 128), item2vec_probe_dim=64)

    assert res["metric"] == "temporal_recall"
    assert res["in_sample"] is False
    failed_rows = [g for g in res["grid"] if g.get("failed")]
    assert len(failed_rows) == 1
    failed = failed_rows[0]
    assert failed["method"] == "svd" and failed["dim"] == 48
    assert failed["recall"] == 0.0
    assert failed["metric"] == "temporal_recall"          # no silent switch to recall_at_k
    # the winner must not be the failed (zeroed) row
    assert not (res["winner"]["method"] == "svd" and res["winner"]["dim"] == 48)
    assert res["winner"]["method"] == "svd" and res["winner"]["dim"] == 96   # 0.6 is the max


def test_all_configs_fail_restores_previous_config_with_sweep_failed(store, monkeypatch):
    """When EVERY grid row fails (temporal metric returns None for all configs), autotune must
    restore and rebuild the PREVIOUS config instead of picking an arbitrary failed row."""
    builds = _stub_build_and_store_tracking_dim(monkeypatch)
    monkeypatch.setattr(eval_recs, "recall_at_k",
                         lambda store, k=20, min_size=5, seed=0: {"recall_at_k": None})

    # Set a known previous config: svd with dim 96
    store.set_setting("rec_embed_method", "svd")
    store.set_setting("rec_dim", "96")

    # Every config in the sweep fails (returns None)
    def _temporal(store, holdout_days=30, k=20):
        if builds["n"] == 0:
            return {"recall": 0.5}   # pre-sweep: metric pick + previous-score calls
        return {"recall": None}     # all swept configs fail

    monkeypatch.setattr(eval_recs, "temporal_recall", _temporal)

    res = eval_recs.autotune(store, svd_dims=(48, 64, 96, 128), item2vec_probe_dim=64)

    # All grid rows must be marked as failed
    assert all(g.get("failed") for g in res["grid"]), "All rows should be marked failed"

    # Result must indicate the sweep failed
    assert res["sweep_failed"] is True

    # Winner should be the previous config, not an arbitrary grid row
    assert res["winner"]["method"] == "svd"
    assert res["winner"]["dim"] == 96
    assert res["winner"] == res["previous"]

    # Settings must be restored to the previous config
    assert store.get_setting("rec_embed_method") == "svd"
    assert store.get_setting("rec_dim") == "96"
