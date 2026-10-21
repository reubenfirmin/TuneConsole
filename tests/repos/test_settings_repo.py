"""DAO suite for SettingsRepo (key/value app settings)."""


def test_get_set_and_default(store):
    assert store.settings.get_setting("k") is None
    assert store.settings.get_setting("k", "fallback") == "fallback"
    store.settings.set_setting("k", "v")
    assert store.settings.get_setting("k") == "v"
    store.settings.set_setting("k", "v2")                # INSERT OR REPLACE
    assert store.settings.get_setting("k") == "v2"


def test_set_none_stores_empty_string(store):
    store.settings.set_setting("k", None)
    assert store.settings.get_setting("k") == ""


def test_setting_persists_on_the_shared_connection(store):
    store.settings.set_setting("lastfm_api_key", "abc")
    assert store.conn.execute("SELECT value FROM settings WHERE key='lastfm_api_key'").fetchone()[0] == "abc"
    assert store.settings.get_setting("lastfm_api_key") == "abc"
