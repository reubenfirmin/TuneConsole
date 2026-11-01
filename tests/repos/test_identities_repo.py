"""DAO suite for AccountRepo (the managed YouTube Music accounts)."""
import pytest
from yt_playlist.core.config import AccountConfig


def test_upsert_is_idempotent_on_label(store):
    a = store.accounts.upsert_account("me", "cred-1", "BA1", True)
    b = store.accounts.upsert_account("me", "cred-2", "BA1", True)
    assert a == b
    ids = store.accounts.get_accounts()
    assert len(ids) == 1 and ids[0].credential_ref == "cred-2" and ids[0].brand_account_id == "BA1"
    assert store.accounts.get_master_account().label == "me"
    assert [i.label for i in ids] == ["me"]
    with pytest.raises(ValueError,match='cannot be retargeted'):
        store.accounts.upsert_account('me','cred-2','BA2',True)


def test_get_master_identity(store):
    store.accounts.upsert_account("alt", "c", None, False)
    store.accounts.upsert_account("main", "c", None, True)
    assert store.accounts.get_master_account().label == "main"


def test_get_master_identity_missing_raises(store):
    store.accounts.upsert_account("alt", "c", None, False)
    with pytest.raises(ValueError):
        store.accounts.get_master_account()


@pytest.mark.parametrize('flag', [2, -1, 1.0, 'false', None])
def test_upsert_rejects_non_boolean_account_flags_before_writing(store, flag):
    before = store.conn.total_changes
    with pytest.raises(ValueError, match='boolean or integer 0 or 1'):
        store.accounts.upsert_account('invalid', 'c', None, flag)
    assert store.conn.total_changes == before


@pytest.mark.parametrize('flags', [(False, False), (True, True), (True, 2)])
def test_reconciliation_validates_master_configuration_before_writing(store, flags):
    existing = store.accounts.upsert_account('existing', 'c', None, True)
    before = store.conn.total_changes
    configs = [AccountConfig(label, 'c', None, flag) for label, flag in zip(('a', 'b'), flags)]
    with pytest.raises(ValueError):
        store.accounts.replace_accounts(configs)
    assert store.conn.total_changes == before
    assert store.accounts.get_master_account().id == existing


def test_master_and_label_exchange_preserves_account_ids_and_retired_history(store):
    configs = [AccountConfig('main', 'c', None, True), AccountConfig('brand', 'c', 'BA1', False)]
    ids = store.accounts.replace_accounts(configs)
    exchanged = [AccountConfig('brand', 'c', None, False, ids['main']),
                 AccountConfig('main', 'c', 'BA1', True, ids['brand'])]
    assert store.accounts.replace_accounts(exchanged) == {'brand': ids['main'], 'main': ids['brand']}
    assert store.accounts.get_master_account().id == ids['brand']
    store.accounts.replace_accounts(exchanged[1:])
    assert store.conn.execute('SELECT active FROM accounts WHERE id=?', (ids['main'],)).fetchone()[0] == 0
    assert store.accounts.replace_accounts(exchanged)['brand'] == ids['main']


def test_reconciliation_allows_temporary_zero_master_but_checks_the_final_state(store):
    config = AccountConfig('main', 'c', None, True)
    ids = store.accounts.replace_accounts([config])
    with store.accounts.reconciled([config]):
        store.conn.execute('UPDATE accounts SET is_master=0')
        store.conn.execute('UPDATE accounts SET is_master=1 WHERE id=?', (ids['main'],))
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM accounts')]
    with pytest.raises(ValueError, match='exactly one active master'):
        with store.accounts.reconciled([config]):
            store.conn.execute('UPDATE accounts SET is_master=0')
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM accounts')] == before


def test_interrupted_master_change_rolls_back_before_configuration_publication(store):
    configs = [AccountConfig('main', 'c', None, True), AccountConfig('brand', 'c', 'BA1', False)]
    ids = store.accounts.replace_accounts(configs)
    switched = [AccountConfig('main', 'c', None, False, ids['main']),
                AccountConfig('brand', 'c', 'BA1', True, ids['brand'])]
    with pytest.raises(RuntimeError, match='controlled interrupted setup'):
        with store.accounts.reconciled(switched):
            assert store.accounts.get_master_account().id == ids['brand']
            raise RuntimeError('controlled interrupted setup')
    assert store.accounts.get_master_account().id == ids['main']


def test_migration_preserves_existing_identities(legacy_store):
    from yt_playlist.core.migrations import migrate
    from yt_playlist.core.migrations.registry import MIGRATIONS

    store = legacy_store
    with store.conn:
        iid = store.conn.execute("INSERT INTO identities(label,credential_ref,is_master) VALUES ('main','cred',1)").lastrowid
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]

    # This genuine v7 fixture requires the explicit historical upgrade route.
    # Preserve its applied history rather than disguising it as unversioned.
    migrate(store.conn, registry=MIGRATIONS)

    store.init_schema()

    assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')][:len(history)] == history
    assert [(i.id, i.label) for i in store.accounts.get_accounts()] == [(iid, "main")]
    assert store.accounts.get_master_account().id == iid
