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

        # This case owns v46's selective deletion. The populated historical-chain
        # owner checks the same values after all subsequent registered upgrades.
        migrate(store.conn, registry=MIGRATIONS[:46])

        settings = dict(store.conn.execute("SELECT key,value FROM settings"))
        assert "rec_param:onboard_coverage_min" not in settings
        assert "rec_param:onboard_library_min" not in settings
        assert settings["rec_param:onboard_feedback_min"] == "15"
        assert settings["onboard_dismissed"] == "1"
        assert float(settings["last_sync_at"]) == 1000
        before = list(store.conn.iterdump())
        migrate(store.conn, registry=MIGRATIONS[:46])
        assert list(store.conn.iterdump()) == before
    finally:
        store.conn.close()
