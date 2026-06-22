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


def test_facade_delegates(store):
    store.set_setting("lastfm_api_key", "abc")           # legacy store.x() call site
    assert store.get_setting("lastfm_api_key") == "abc"
