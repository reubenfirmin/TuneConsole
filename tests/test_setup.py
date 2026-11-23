import pytest
from yt_playlist.core.config import account_configs, validate_accounts


def test_validate_identities_rules():
    ok = [{"label": "main", "is_master": True}, {"label": "brand", "is_master": False}]
    validate_accounts(ok)  # no raise

    with pytest.raises(ValueError, match="at least one"):
        validate_accounts([])
    with pytest.raises(ValueError, match="needs a label"):
        validate_accounts([{"label": "  ", "is_master": True}])
    with pytest.raises(ValueError, match="unique"):
        validate_accounts([{"label": "a", "is_master": True}, {"label": "a", "is_master": False}])
    with pytest.raises(ValueError, match="exactly one"):
        validate_accounts([{"label": "a", "is_master": False}])
    with pytest.raises(ValueError, match="exactly one"):
        validate_accounts([{"label": "a", "is_master": True}, {"label": "b", "is_master": True}])


@pytest.mark.parametrize("value", ['line\nnext', 'tab\tstop', 'carriage\rreturn',
    'null\x00byte', 'bell\x07byte', 'delete\x7fbyte', 'quote"slash\\', 'music 🎵'])
def test_account_setup_preserves_supported_text(value):
    cfg, = account_configs([{"label": value, "brand_account_id": value, "is_master": True}])
    assert cfg.label == cfg.brand_account_id == value
