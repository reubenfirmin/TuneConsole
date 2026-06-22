import pytest
from yt_playlist.core.config import load_identities
from yt_playlist.util.retry import with_retry

def test_load_identities_requires_one_master(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[identity]]\nlabel="main"\ncredential_ref="cred.json"\nis_master=true\n'
        '[[identity]]\nlabel="brand"\ncredential_ref="cred.json"\nbrand_account_id="123"\n')
    ids = load_identities(p)
    assert len(ids) == 2
    assert sum(i.is_master for i in ids) == 1
    assert ids[1].brand_account_id == "123"

def test_load_identities_rejects_zero_masters(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[[identity]]\nlabel="a"\ncredential_ref="c"\n')
    with pytest.raises(ValueError):
        load_identities(p)

def test_load_identities_rejects_multiple_masters(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[identity]]\nlabel="a"\ncredential_ref="c"\nis_master=true\n'
        '[[identity]]\nlabel="b"\ncredential_ref="c"\nis_master=true\n')
    with pytest.raises(ValueError):
        load_identities(p)

def test_with_retry_retries_then_succeeds():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"
    assert with_retry(flaky, attempts=3, base_delay=0, sleep=lambda _: None) == "ok"
    assert calls["n"] == 3


def test_credential_path_inside_base_ok(tmp_path):
    from yt_playlist.core.config import credential_path
    (tmp_path / "cred.json").write_text("{}")
    assert credential_path(tmp_path, "cred.json") == (tmp_path / "cred.json").resolve()

def test_credential_path_rejects_traversal(tmp_path):
    from yt_playlist.core.config import credential_path
    with pytest.raises(ValueError):
        credential_path(tmp_path, "../../../../etc/passwd")

def test_credential_path_rejects_absolute(tmp_path):
    from yt_playlist.core.config import credential_path
    with pytest.raises(ValueError):
        credential_path(tmp_path, "/etc/passwd")
