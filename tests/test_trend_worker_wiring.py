"""A rebuild materializes local views and continues when a best-effort stage fails."""
import logging
from types import SimpleNamespace

import pytest

from yt_playlist.rec import (
    actions, artist_model, discover, embed, into_recently, mode_surfaces,
    model_selection, rec_worker, taste_modes, trend_rollups,
)


@pytest.mark.parametrize("module,method,warning", [
    (taste_modes, "recompute", "taste-modes recompute failed"),
    (mode_surfaces, "prepare_bundles", "mode-bundle prep failed"),
    (trend_rollups, "build", "trend-rollup build failed"),
], ids=["taste-modes", "mode-bundles", "trend-rollups"])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_rebuild_materializes_views_under_a_guard(store, monkeypatch, caplog, module, method, warning, fail):
    monkeypatch.setattr(embed, "build_and_store", lambda store: 0)
    monkeypatch.setattr(embed, "maybe_rebuild_content_vectors", lambda store: False)
    monkeypatch.setattr(model_selection, "maintain", lambda *args, **kwargs: None)
    monkeypatch.setattr(taste_modes, "recompute", lambda store, now: 0)
    monkeypatch.setattr(mode_surfaces, "prepare_bundles", lambda store, now: {})
    monkeypatch.setattr(trend_rollups, "build", lambda store, now: {})
    monkeypatch.setattr(artist_model, "build_artist_model_and_store", lambda store: 0)
    monkeypatch.setattr(into_recently, "prewarm_pool", lambda store, now: 0)
    monkeypatch.setattr(actions, "refresh_cleanup", lambda store, now: {"count": 0})
    calls, completed = [], []

    def materialize(actual_store, now):
        calls.append((actual_store, now))
        if fail:
            raise RuntimeError("injected materialization failure")
        return 0

    def discovery(ctx, now):
        completed.append((ctx, now))
        return {"scanned": 0}

    monkeypatch.setattr(module, method, materialize)
    monkeypatch.setattr(discover, "run_discovery", discovery)
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1234.0, logger=logging.getLogger("test.rebuild"))
    worker = rec_worker.RecWorker(ctx)
    with caplog.at_level(logging.WARNING, logger=ctx.logger.name):
        worker.rebuild()

    assert calls == [(store, 1234.0)]
    assert completed == [(ctx, 1234.0)]
    assert not worker.busy and not worker.work.active
    assert caplog.messages == ([f"rec rebuild: {warning}"] if fail else [])
