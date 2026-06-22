import os
from yt_playlist.core import paths

def test_paths_honor_home_override(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert paths.db_path() == tmp_path / "state.db"
    assert paths.backups_dir() == tmp_path / "backups"
    assert paths.backups_dir().is_dir()  # created on access
