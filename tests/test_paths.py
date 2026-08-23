import os
from yt_playlist.core import paths

def test_paths_honor_home_override(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert paths.db_path() == tmp_path / "state.db"
    assert paths.backups_dir() == tmp_path / "backups"
    assert paths.backups_dir().is_dir()  # created on access


def test_macos_uses_native_application_support_and_logs(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_PLAYLIST_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))

    support = tmp_path / "Library" / "Application Support" / "TuneConsole"
    assert paths.data_dir() == support
    assert paths.config_path() == support / "config.toml"
    assert paths.logs_dir() == tmp_path / "Library" / "Logs" / "TuneConsole"


def test_macos_honors_explicit_xdg_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_PLAYLIST_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.data_dir() == tmp_path / "xdg-data" / "yt-playlist"
    assert paths.config_path() == tmp_path / "xdg-config" / "yt-playlist" / "config.toml"
