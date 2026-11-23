"""Verify account import completion on a populated private library upgrade."""
from contextlib import closing

from yt_playlist.core import runtime
from yt_playlist.core.migrations import account_binding_receipt, regular
from yt_playlist.core.store import Store


def test_actual_upgrade_preserves_account_bindings_and_import_completion(managed_library, tmp_path, monkeypatch):
    change = next(change for change in regular.CHANGES if change.name == account_binding_receipt.NAME)
    store = Store(managed_library)
    columns = 'id,label,brand_account_id,is_master,active'
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:change.version - 1])
        accounts = [tuple(row) for row in conn.execute(f'SELECT {columns} FROM accounts ORDER BY id')]
        assert any(row[-1] for row in accounts)
        with conn.atomic():
            conn.execute("INSERT INTO settings(key,value) VALUES (?,'1') "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (account_binding_receipt.OLD_KEY,))
        preferences = dict(conn.execute('SELECT key,value FROM settings WHERE key<>?',
                                        (account_binding_receipt.OLD_KEY,)))

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        config = tmp_path / 'config.toml'
        config.write_text('[[identity]]\nlabel="Old configuration"\nis_master=true\n')
        monkeypatch.setattr(runtime, 'build_client', lambda *_: object())
        app_runtime = runtime.Runtime(store, config)
        app_runtime.load()

        assert store.accounts.bindings_saved()
        assert set(app_runtime.clients()) == {row[0] for row in accounts if row[-1]}
        assert [tuple(row) for row in conn.execute(f'SELECT {columns} FROM accounts ORDER BY id')] == accounts
        assert dict(conn.execute('SELECT key,value FROM settings')) == preferences
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
