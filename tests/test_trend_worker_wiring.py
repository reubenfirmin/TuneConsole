"""Failed fits retain the published model; auxiliary failures retain their own retry state."""
import logging
from types import SimpleNamespace

import pytest

from yt_playlist.rec import (
    mode_surfaces, model_refresh, model_selection, rec_worker,
)


@pytest.mark.parametrize("module,method,warning", [
    (model_refresh, "rebuild", None),
    (model_selection, "maintain", "automatic model comparison failed"),
    (mode_surfaces, "prepare_bundles", "rec bundle preparation failed"),
], ids=["models", "model-selection", "mode-bundles"])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_rebuild_materializes_views_under_a_guard(store, monkeypatch, caplog, module, method, warning, fail):
    monkeypatch.setattr(model_refresh, "rebuild", lambda *args, **kwargs: {})
    monkeypatch.setattr(model_selection, "maintain", lambda *args, **kwargs: None)
    monkeypatch.setattr(mode_surfaces, "prepare_bundles", lambda store, now: {})
    calls = []

    def materialize(actual_store, now, **kwargs):
        calls.append((actual_store, now))
        if fail:
            raise RuntimeError("injected materialization failure")
        return {}

    monkeypatch.setattr(module, method, materialize)
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1234.0, logger=logging.getLogger("test.rebuild"))
    worker = rec_worker.RecWorker(ctx)
    monkeypatch.setattr(worker, "trigger_home_refresh", lambda: None)
    with caplog.at_level(logging.WARNING, logger=ctx.logger.name):
        if fail and module is model_refresh:
            with pytest.raises(RuntimeError, match="injected materialization failure"):
                worker.rebuild()
        else:
            worker.rebuild()

    assert calls == [(store, 1234.0)]
    assert not worker.busy and not worker.work.active
    assert worker.jobs.retry['bundles'].dirty == (fail and module is mode_surfaces)
    if fail and warning:
        assert len(caplog.messages) == 1 and warning in caplog.messages[0]
    else:
        assert not caplog.messages
