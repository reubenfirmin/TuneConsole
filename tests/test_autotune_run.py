import json

import pytest

from yt_playlist.rec import autotune_run


def test_last_result_reads_saved_report_without_writing(store, monkeypatch):
    report = {"ran_at": 1000.0, "winner": {"method": "svd", "dim": 48}}
    store.settings.set_setting(autotune_run.RESULT_SETTING, json.dumps(report))

    def unexpected_write(*args, **kwargs):
        pytest.fail("Reading a historical report must not write settings")

    monkeypatch.setattr(store.settings, "set_setting", unexpected_write)
    assert autotune_run.last_result(store) == report


@pytest.mark.parametrize("raw", [None, "", "broken json", "null", "[]", "42"])
def test_last_result_absent_or_invalid(store, raw):
    if raw is not None:
        store.settings.set_setting(autotune_run.RESULT_SETTING, raw)
    assert autotune_run.last_result(store) is None
