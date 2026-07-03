import sys
import pytest
from yt_playlist.core import updatecheck as uc


class FakeStore:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
    def get_setting(self, key, default=None):
        return self.settings.get(key, default)
    def set_setting(self, key, value):
        self.settings[key] = value or ""


def test_current_version_strips_dev_suffix(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.1.6.dev3+gabc1234")
    assert uc.current_version() == "0.1.6"
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.2.0")
    assert uc.current_version() == "0.2.0"


def test_install_kind_flatpak(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.tuneconsole.TuneConsole")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert uc.install_kind() == "flatpak"


def test_install_kind_macos(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert uc.install_kind() == "macos"


def test_install_kind_pip(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert uc.install_kind() == "pip"


def test_update_instruction_per_kind():
    assert uc.update_instruction("flatpak") == (
        "flatpak update --user com.tuneconsole.TuneConsole", None)
    label, link = uc.update_instruction("macos")
    assert label == "Get the latest release"
    assert link == "https://github.com/reubenfirmin/TuneConsole/releases/latest"
    assert uc.update_instruction("pip") == ("pip install -U yt-playlist", None)


def test_update_nudge_none_when_no_latest_seen(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.1.6")
    assert uc.update_nudge(FakeStore()) is None


def test_update_nudge_none_when_current_is_latest(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.2.0")
    store = FakeStore({"latest_version_seen": "0.2.0"})
    assert uc.update_nudge(store) is None


def test_update_nudge_present_when_behind(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.1.6")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    store = FakeStore({"latest_version_seen": "0.2.0"})
    nudge = uc.update_nudge(store)
    assert nudge == {"current": "0.1.6", "latest": "0.2.0",
                     "kind": "pip", "command": "pip install -U yt-playlist", "link": None}


def test_update_nudge_dev_build_on_latest_does_not_nag(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.2.0.dev4+gdead")
    store = FakeStore({"latest_version_seen": "0.2.0"})
    assert uc.update_nudge(store) is None


def test_update_nudge_suppressed_for_dismissed_version(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.1.6")
    store = FakeStore({"latest_version_seen": "0.2.0",
                       "backend_update_dismissed_version": "0.2.0"})
    assert uc.update_nudge(store) is None


def test_update_nudge_returns_after_newer_release_than_dismissed(monkeypatch):
    monkeypatch.setattr(uc, "_raw_version", lambda: "0.1.6")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    store = FakeStore({"latest_version_seen": "0.3.0",
                       "backend_update_dismissed_version": "0.2.0"})
    assert uc.update_nudge(store)["latest"] == "0.3.0"


def test_check_latest_fetches_and_caches_when_never_checked(monkeypatch):
    monkeypatch.setattr(uc, "_fetch_latest_release", lambda: "0.2.0")
    store = FakeStore()
    uc.check_latest(store, now=1000.0)
    assert store.get_setting("latest_version_seen") == "0.2.0"
    assert store.get_setting("latest_version_checked_at") == "1000.0"


def test_check_latest_skips_when_checked_recently(monkeypatch):
    calls = []
    monkeypatch.setattr(uc, "_fetch_latest_release", lambda: calls.append(1) or "0.9.9")
    store = FakeStore({"latest_version_checked_at": "1000.0", "latest_version_seen": "0.2.0"})
    uc.check_latest(store, now=1000.0 + 3600, interval_s=86400)   # only 1h later
    assert calls == []                                            # not fetched
    assert store.get_setting("latest_version_seen") == "0.2.0"    # unchanged


def test_check_latest_refetches_after_interval(monkeypatch):
    monkeypatch.setattr(uc, "_fetch_latest_release", lambda: "0.3.0")
    store = FakeStore({"latest_version_checked_at": "1000.0", "latest_version_seen": "0.2.0"})
    uc.check_latest(store, now=1000.0 + 86400 + 1, interval_s=86400)
    assert store.get_setting("latest_version_seen") == "0.3.0"


def test_check_latest_fail_silent_keeps_cached_value(monkeypatch):
    def boom():
        raise OSError("offline")
    monkeypatch.setattr(uc, "_fetch_latest_release", boom)
    store = FakeStore({"latest_version_seen": "0.2.0"})
    uc.check_latest(store, now=5000.0)                            # must not raise
    assert store.get_setting("latest_version_seen") == "0.2.0"   # cached value retained
    assert store.get_setting("latest_version_checked_at") == "5000.0"  # still stamped (bounds retries)


def test_maybe_check_update_swallows_errors(monkeypatch):
    from yt_playlist.web import app as webapp

    class Ctx:
        store = FakeStore()
        now_fn = staticmethod(lambda: 1000.0)
        class logger:
            @staticmethod
            def warning(*a, **k): pass

    def boom(store, now):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(webapp.updatecheck, "check_latest", boom)
    webapp._maybe_check_update(Ctx)          # must not raise


def test_maybe_check_update_invokes_check(monkeypatch):
    from yt_playlist.web import app as webapp
    seen = {}

    class Ctx:
        store = FakeStore()
        now_fn = staticmethod(lambda: 4242.0)
        class logger:
            @staticmethod
            def warning(*a, **k): pass

    monkeypatch.setattr(webapp.updatecheck, "check_latest",
                        lambda store, now: seen.update(store=store, now=now))
    webapp._maybe_check_update(Ctx)
    assert seen["now"] == 4242.0
    assert seen["store"] is Ctx.store
