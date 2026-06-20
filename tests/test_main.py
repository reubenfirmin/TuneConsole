import pytest
from yt_playlist.__main__ import sync_identities_into_store, validate_credentials
from yt_playlist.config import IdentityConfig

def test_sync_identities_into_store_inserts_once(store):
    cfgs = [IdentityConfig("main", "cred.json", None, True),
            IdentityConfig("brand", "cred.json", "999", False)]
    ids = sync_identities_into_store(store, cfgs)
    assert len(ids) == 2
    # idempotent: calling again does not duplicate
    ids2 = sync_identities_into_store(store, cfgs)
    assert len(store.get_identities()) == 2
    assert ids == ids2

def test_validate_credentials_missing_raises(tmp_path):
    cfgs = [IdentityConfig("main", "cred.json", None, True)]
    with pytest.raises(SystemExit) as exc_info:
        validate_credentials(cfgs, base_dir=tmp_path)
    assert "cred.json" in str(exc_info.value)
    assert "main" in str(exc_info.value)

def test_validate_credentials_present_passes(tmp_path):
    cred_file = tmp_path / "cred.json"
    cred_file.write_text("{}")
    cfgs = [IdentityConfig("main", "cred.json", None, True)]
    # Should not raise
    validate_credentials(cfgs, base_dir=tmp_path)
