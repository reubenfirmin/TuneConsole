"""DAO suite for IdentityRepo (the managed YouTube Music accounts)."""
import pytest


def test_upsert_is_idempotent_on_label(store):
    a = store.identities.upsert_identity("me", "cred-1", "BA1", True)
    b = store.identities.upsert_identity("me", "cred-2", "BA1", True)
    assert a == b
    ids = store.identities.get_identities()
    assert len(ids) == 1 and ids[0].credential_ref == "cred-2" and ids[0].brand_account_id == "BA1"
    with pytest.raises(ValueError,match='cannot be retargeted'):
        store.identities.upsert_identity('me','cred-2','BA2',True)


def test_get_master_identity(store):
    store.identities.upsert_identity("alt", "c", None, False)
    store.identities.upsert_identity("main", "c", None, True)
    assert store.identities.get_master_identity().label == "main"


def test_get_master_identity_missing_raises(store):
    store.identities.upsert_identity("alt", "c", None, False)
    with pytest.raises(ValueError):
        store.identities.get_master_identity()


def test_store_binding(store):
    store.identities.upsert_identity("me", "c", None, True)
    assert store.identities.get_master_identity().label == "me"
    assert [i.label for i in store.identities.get_identities()] == ["me"]


def test_migration_preserves_existing_identities(legacy_store):
    store = legacy_store
    iid = store.identities.upsert_identity("main", "cred", None, True)
    with store.conn:
        store.conn.execute("ALTER TABLE identities DROP COLUMN active")
        store.conn.execute("DROP TABLE schema_migrations")

    store.init_schema()

    assert [(i.id, i.label) for i in store.identities.get_identities()] == [(iid, "main")]
    assert store.identities.get_master_identity().id == iid
