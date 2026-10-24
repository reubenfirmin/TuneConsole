"""Retiring unsupported controls preserves the active feedback control and setup state."""
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def test_upgrade_removes_only_the_unsupported_onboarding_overrides():
    store = Store(":memory:")
    try:
        migrate(store.conn, registry=MIGRATIONS[:45])
        store.conn.executemany("INSERT INTO settings(key,value) VALUES (?,?)", [
            ("rec_param:onboard_coverage_min", "0.9"),
            ("rec_param:onboard_library_min", "200"),
            ("rec_param:onboard_feedback_min", "15"),
            ("onboard_dismissed", "1"),
            ("last_sync_at", "1000"),
        ])
        store.conn.commit()

        store.init_schema()

        assert store.settings.get_setting("rec_param:onboard_coverage_min") is None
        assert store.settings.get_setting("rec_param:onboard_library_min") is None
        assert store.settings.get_setting("rec_param:onboard_feedback_min") == "15"
        assert store.settings.get_setting("onboard_dismissed") == "1"
        assert store.settings.get_setting("last_sync_at") == "1000"
    finally:
        store.conn.close()
