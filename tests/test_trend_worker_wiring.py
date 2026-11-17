"""Failed fits retain the published model; auxiliary failures retain their own retry state."""
import logging
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.rec import (
    model_refresh, model_selection, rec_worker,
)
from yt_playlist.library import mode_composition


@pytest.mark.parametrize("module,method,warning", [
    (model_refresh, "rebuild", None),
    (model_selection, "maintain", "automatic model comparison failed"),
    (mode_composition, "preparation", "rec bundle preparation failed"),
], ids=["models", "model-selection", "mode-bundles"])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_rebuild_materializes_views_under_a_guard(store, monkeypatch, caplog, module, method, warning, fail):
    monkeypatch.setattr(model_refresh, "rebuild", lambda *args, **kwargs: {})
    monkeypatch.setattr(model_selection, "maintain", lambda *args, **kwargs: None)
    monkeypatch.setattr(mode_composition, "preparation", lambda owner:
        SimpleNamespace(prepare=lambda now: {}))
    calls = []

    def materialize(actual_store, now, **kwargs):
        calls.append((actual_store, now))
        logging.getLogger('yt_playlist.database').warning('Slow SQL: unrelated diagnostic during rebuild')
        if fail:
            raise RuntimeError("injected materialization failure")
        return {}

    if module is mode_composition:
        monkeypatch.setattr(module, method, lambda owner:
            SimpleNamespace(prepare=lambda now: materialize(owner, now)))
    else:
        monkeypatch.setattr(module, method, materialize)
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1234.0, logger=logging.getLogger("test.rebuild"))
    worker = rec_worker.RecWorker(ctx, debounce_s=0)
    monkeypatch.setattr(worker, "trigger_home_refresh", lambda: None)
    groups_started, release_groups = threading.Event(), threading.Event()

    def prepare_groups():
        groups_started.set()
        assert release_groups.wait(5)

    monkeypatch.setattr(worker, '_prepare_groups', prepare_groups)
    try:
        with caplog.at_level(logging.WARNING, logger=ctx.logger.name):
            if fail and module is model_refresh:
                with pytest.raises(RuntimeError, match="injected materialization failure"):
                    worker.rebuild()
            else:
                worker.rebuild()
        # Successful bundles hand off shadow grouping after the direct rebuild.
        # It owns Activity while queued/running, without showing a model spinner.
        handoff = not (fail and module in (model_refresh, mode_composition))
        if handoff:
            assert groups_started.wait(5)
            assert not worker.busy
            assert worker.work.active == ('recommendation updates',)
            assert not worker.work.wait(0)
        else:
            assert not groups_started.is_set() and not worker.work.active
        assert worker.jobs.retry['bundles'].dirty == (fail and module is mode_composition)
    finally:
        release_groups.set()
        worker.shutdown()
        worker.join(5)

    assert calls == [(store, 1234.0)]
    assert not worker.busy and not worker.work.active
    assert worker.work.wait(0)
    assert ('yt_playlist.database', logging.WARNING,
            'Slow SQL: unrelated diagnostic during rebuild') in caplog.record_tuples
    records = [record for record in caplog.records if record.name == ctx.logger.name]
    if fail and warning:
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert warning in records[0].getMessage()
    else:
        assert not records
