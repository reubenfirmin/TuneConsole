import os
from pathlib import Path

# Resolution order for both config and data:
#   1. $YT_PLAYLIST_HOME                 — explicit override (tests, custom installs)
#   2. $XDG_CONFIG_HOME / $XDG_DATA_HOME — honoured so sandboxes (Flatpak) that redirect these to a
#      per-app directory work with no host filesystem access granted
#   3. ~/.config and ~/.local/share      — the usual fallbacks


def _xdg_base(xdg_var, default_subpath) -> Path:
    xdg = os.environ.get(xdg_var)
    return Path(xdg) if xdg else Path.home() / default_subpath


def data_dir() -> Path:
    override = os.environ.get("YT_PLAYLIST_HOME")
    base = Path(override) if override else _xdg_base("XDG_DATA_HOME", ".local/share") / "yt-playlist"
    base.mkdir(parents=True, exist_ok=True)
    return base

def db_path() -> Path:
    return data_dir() / "state.db"

def backups_dir() -> Path:
    d = data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d

def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d

def network_log_path() -> Path:
    """Rotating egress log written by the network guard (see yt_playlist.egress)."""
    return logs_dir() / "network.log"

def config_path() -> Path:
    override = os.environ.get("YT_PLAYLIST_HOME")
    if override:
        return Path(override) / "config.toml"
    d = _xdg_base("XDG_CONFIG_HOME", ".config") / "yt-playlist"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.toml"
