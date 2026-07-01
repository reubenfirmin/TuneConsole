import pytest
from yt_playlist.core import setup as setup_mod
from yt_playlist.core.config import load_identities


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
