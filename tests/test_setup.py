import pytest
from yt_playlist import setup as setup_mod
from yt_playlist.config import load_identities


def test_store_credentials_writes_validated_blob(monkeypatch, tmp_path):
    import ytmusicapi
    monkeypatch.setattr(ytmusicapi, "setup", lambda headers_raw=None: '{"cookie": "x"}')
    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda blob: object())  # no network
    dest = tmp_path / "browser.json"
    setup_mod.store_credentials("Cookie: SID=abc\nX-Goog: 1", dest)
    assert dest.read_text() == '{"cookie": "x"}'


def test_store_credentials_blank_raises(tmp_path):
    with pytest.raises(ValueError, match="Copy as cURL"):
        setup_mod.store_credentials("   ", tmp_path / "browser.json")


def test_store_credentials_bad_headers_raises(monkeypatch, tmp_path):
    import ytmusicapi
    def boom(headers_raw=None):
        raise Exception("no cookie found")
    monkeypatch.setattr(ytmusicapi, "setup", boom)
    with pytest.raises(ValueError, match="could not read those headers"):
        setup_mod.store_credentials("garbage", tmp_path / "browser.json")


def test_normalize_capture_passthrough_raw_headers():
    raw = "cookie: SID=abc\nx-goog-authuser: 0"
    assert setup_mod.normalize_capture(raw) == raw

def test_normalize_capture_from_curl():
    curl = (
        "curl 'https://music.youtube.com/youtubei/v1/browse' \\\n"
        "  -H 'cookie: SID=abc; HSID=def' \\\n"
        "  -H 'x-goog-authuser: 0' \\\n"
        "  --data-raw '{}'")
    out = setup_mod.normalize_capture(curl)
    assert "cookie: SID=abc; HSID=def" in out
    assert "x-goog-authuser: 0" in out

def test_normalize_capture_from_curl_b_cookie_flag():
    curl = "curl 'https://music.youtube.com/browse' -b 'SID=abc' -H 'x-goog-authuser: 0'"
    out = setup_mod.normalize_capture(curl)
    assert "cookie: SID=abc" in out
    assert "x-goog-authuser: 0" in out

def test_validate_identities_rules():
    ok = [{"label": "main", "is_master": True}, {"label": "brand", "is_master": False}]
    setup_mod.validate_identities(ok)  # no raise

    with pytest.raises(ValueError, match="at least one"):
        setup_mod.validate_identities([])
    with pytest.raises(ValueError, match="needs a label"):
        setup_mod.validate_identities([{"label": "  ", "is_master": True}])
    with pytest.raises(ValueError, match="unique"):
        setup_mod.validate_identities([{"label": "a", "is_master": True}, {"label": "a", "is_master": False}])
    with pytest.raises(ValueError, match="exactly one"):
        setup_mod.validate_identities([{"label": "a", "is_master": False}])
    with pytest.raises(ValueError, match="exactly one"):
        setup_mod.validate_identities([{"label": "a", "is_master": True}, {"label": "b", "is_master": True}])


def test_write_config_roundtrips(tmp_path):
    identities = [
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC123", "is_master": False},
    ]
    path = tmp_path / "config.toml"
    setup_mod.write_config(identities, path)
    cfgs = load_identities(path)
    assert [c.label for c in cfgs] == ["main", "brand"]
    assert sum(c.is_master for c in cfgs) == 1
    main = next(c for c in cfgs if c.label == "main")
    brand = next(c for c in cfgs if c.label == "brand")
    assert main.brand_account_id is None and main.is_master
    assert brand.brand_account_id == "UC123" and not brand.is_master


def test_write_config_escapes_quotes(tmp_path):
    identities = [{"label": 'we"ird', "credential_ref": "browser.json",
                   "brand_account_id": None, "is_master": True}]
    path = tmp_path / "config.toml"
    setup_mod.write_config(identities, path)
    cfgs = load_identities(path)
    assert cfgs[0].label == 'we"ird'


def test_write_config_rejects_no_master(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        setup_mod.write_config(
            [{"label": "a", "credential_ref": "browser.json", "brand_account_id": None, "is_master": False}],
            tmp_path / "config.toml")


def test_verify_capture_returns_account(monkeypatch):
    import ytmusicapi, types
    monkeypatch.setattr(ytmusicapi, "setup", lambda headers_raw=None: '{"cookie":"x"}')
    monkeypatch.setattr(ytmusicapi, "YTMusic",
                        lambda *a, **k: types.SimpleNamespace(get_account_info=lambda: {"accountName": "Reuben"}))
    blob, name = setup_mod.verify_capture("cookie: x\nx-goog-authuser: 0")
    assert blob == '{"cookie":"x"}' and name == "Reuben"

def test_verify_capture_auth_failure(monkeypatch):
    import ytmusicapi, types
    monkeypatch.setattr(ytmusicapi, "setup", lambda headers_raw=None: '{"cookie":"x"}')
    def boom():
        raise Exception("401 Unauthorized")
    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda *a, **k: types.SimpleNamespace(get_account_info=boom))
    with pytest.raises(ValueError, match="sign-in didn't work"):
        setup_mod.verify_capture("cookie: x")

def test_verify_capture_blank_raises():
    with pytest.raises(ValueError, match="provide a sign-in capture"):
        setup_mod.verify_capture("   ")
