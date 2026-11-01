import pytest
from yt_playlist.core.config import load_accounts, validate_accounts
from yt_playlist.util.retry import with_retry

def test_load_identities_requires_one_master(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[identity]]\nlabel="main"\ncredential_ref="cred.json"\nis_master=true\n'
        '[[identity]]\nlabel="brand"\ncredential_ref="cred.json"\nbrand_account_id="123"\n')
    ids = load_accounts(p)
    assert len(ids) == 2
    assert sum(i.is_master for i in ids) == 1
    assert ids[1].brand_account_id == "123"

def test_load_identities_rejects_zero_masters(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[[identity]]\nlabel="a"\ncredential_ref="c"\n')
    with pytest.raises(ValueError):
        load_accounts(p)

def test_load_identities_rejects_multiple_masters(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[identity]]\nlabel="a"\ncredential_ref="c"\nis_master=true\n'
        '[[identity]]\nlabel="b"\ncredential_ref="c"\nis_master=true\n')
    with pytest.raises(ValueError):
        load_accounts(p)


@pytest.mark.parametrize('flag', [2, -1, 1.0, 0.0, 'true', 'false', '', None, [], {}])
def test_account_flags_reject_truthy_and_numeric_coercion(flag):
    with pytest.raises(ValueError, match='boolean or integer 0 or 1'):
        validate_accounts([{'label': 'main', 'credential_ref': 'c', 'is_master': flag}])


@pytest.mark.parametrize('flag', ['"false"', '"true"', '2', '-1', '1.0'])
def test_toml_account_flag_is_validated_before_bool_conversion(tmp_path, flag):
    path = tmp_path / 'config.toml'
    path.write_text(f'[[identity]]\nlabel="main"\ncredential_ref="c"\nis_master={flag}\n')
    with pytest.raises(ValueError, match='boolean or integer 0 or 1'):
        load_accounts(path)


def test_integer_account_flags_load_as_booleans_without_changing_ids(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[[identity]]\nid=17\nlabel="main"\ncredential_ref="c"\nis_master=1\n'
                    '[[identity]]\nid=21\nlabel="brand"\ncredential_ref="c"\nis_master=0\n')
    configs = load_accounts(path)
    assert [(config.account_id, config.is_master) for config in configs] == [(17, True), (21, False)]
    assert all(type(config.is_master) is bool for config in configs)

def test_with_retry_retries_then_succeeds():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"
    assert with_retry(flaky, attempts=3, base_delay=0, sleep=lambda _: None) == "ok"
    assert calls["n"] == 3


def test_retry_warning_summarizes_response_and_keeps_debug_traceback(caplog):
    import logging
    message = ("Unable to find 'contents' using path ['contents', 'tabs'] on "
               "{'responseContext': {'visitorData': 'RAW_RESPONSE_MARKER'}}, exception: 'contents'")
    failure = KeyError(message)
    delays = []

    def fail():
        raise failure

    with caplog.at_level(logging.DEBUG, logger="yt_playlist.util.retry"):
        with pytest.raises(KeyError) as caught:
            with_retry(fail, operation="fetch playlist Mix (PL1), account main", sleep=delays.append)

    assert caught.value is failure
    assert delays == [0.5, 1.0]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "account main: attempt 1/3" in warnings[0]
    assert "retrying in 0.5s" in warnings[0]
    assert "retrying in 1.0s" in warnings[1]
    assert all("KeyError: Unable to find 'contents'" in line for line in warnings)
    assert all("RAW_RESPONSE_MARKER" not in line for line in warnings)
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug) == 3
    assert "attempt 3/3" in debug[-1].getMessage()
    assert debug[-1].exc_info[1] is failure
    assert "RAW_RESPONSE_MARKER" in caplog.text


def test_error_summary_bounds_and_flattens_generic_errors():
    from yt_playlist.util.retry import error_summary
    summary = error_summary(RuntimeError("first line\n" + "x" * 1000))
    assert summary.startswith("RuntimeError: first line ")
    assert "\n" not in summary
    assert summary.endswith("...")
    assert len(summary) <= 420
