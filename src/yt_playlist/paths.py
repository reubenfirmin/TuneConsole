import os
from pathlib import Path

def data_dir() -> Path:
    override = os.environ.get("YT_PLAYLIST_HOME")
    base = Path(override) if override else Path.home() / ".local/share/yt-playlist"
    base.mkdir(parents=True, exist_ok=True)
    return base

def db_path() -> Path:
    return data_dir() / "state.db"

def backups_dir() -> Path:
    d = data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d

def config_path() -> Path:
    override = os.environ.get("YT_PLAYLIST_HOME")
    if override:
        return Path(override) / "config.toml"
    d = Path.home() / ".config/yt-playlist"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.toml"
