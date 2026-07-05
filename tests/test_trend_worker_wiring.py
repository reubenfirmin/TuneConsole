import inspect
from yt_playlist.rec import rec_worker


def test_rebuild_calls_trend_rollups_guarded():
    src = inspect.getsource(rec_worker)
    assert "trend_rollups.build(store, now)" in src
    # guarded like the other best-effort steps
    assert "trend-rollup build failed" in src
