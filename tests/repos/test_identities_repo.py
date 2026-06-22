"""DAO suite for IdentityRepo (the managed YouTube Music accounts)."""
import pytest


def test_upsert_is_idempotent_on_label(store):
    a = store.identities.upsert_identity("me", "cred-1", "BA1", True)
    b = store.identities.upsert_identity("me", "cred-2", "BA2", True)   # same label → updates in place
    assert a == b
    ids = store.identities.get_identities()
    assert len(ids) == 1 and ids[0].credential_ref == "cred-2" and ids[0].brand_account_id == "BA2"


def test_get_master_identity(store):
    store.identities.upsert_identity("alt", "c", None, False)
    store.identities.upsert_identity("main", "c", None, True)
    assert store.identities.get_master_identity().label == "main"


def test_get_master_identity_missing_raises(store):
    store.identities.upsert_identity("alt", "c", None, False)
    with pytest.raises(ValueError):
        store.identities.get_master_identity()


def test_facade_delegates(store):
    store.upsert_identity("me", "c", None, True)                        # legacy store.x() call site
    assert store.get_master_identity().label == "me"
    assert [i.label for i in store.get_identities()] == ["me"]
