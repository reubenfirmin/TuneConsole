import os
import sys
from pathlib import Path

# Resolution order for both config and data:
#   1. $YT_PLAYLIST_HOME                 : explicit override (tests, custom installs)
#   2. $XDG_CONFIG_HOME / $XDG_DATA_HOME : honoured so sandboxes (Flatpak) that redirect these to a
#      per-app directory work with no host filesystem access granted
#   3. macOS: ~/Library/Application Support/TuneConsole and ~/Library/Logs/TuneConsole
#   4. other platforms: ~/.config and ~/.local/share


def _xdg_base(xdg_var, default_subpath) -> Path:
    xdg = os.environ.get(xdg_var)
    return Path(xdg) if xdg else Path.home() / default_subpath


def _native_macos() -> bool:
    return sys.platform == "darwin" and not os.environ.get("YT_PLAYLIST_HOME")


def _mac_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "TuneConsole"


def data_dir() -> Path:
    override = os.environ.get("YT_PLAYLIST_HOME")
    if override:
        base = Path(override)
    elif _native_macos() and not os.environ.get("XDG_DATA_HOME"):
        base = _mac_support_dir()
    else:
        base = _xdg_base("XDG_DATA_HOME", ".local/share") / "yt-playlist"
    base.mkdir(parents=True, exist_ok=True)
    return base

def db_path() -> Path:
    return data_dir() / "state.db"

def backups_dir() -> Path:
    d = data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d

def logs_dir() -> Path:
    if _native_macos() and not os.environ.get("XDG_DATA_HOME"):
        d = Path.home() / "Library" / "Logs" / "TuneConsole"
    else:
        d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d

def network_log_path() -> Path:
    """Rotating egress log written by the network guard (see yt_playlist.egress)."""
    return logs_dir() / "network.log"

def app_log_path() -> Path:
    """Rotating application log (see yt_playlist.core.logsetup). The packaged builds have no
    terminal, so this file is the only place their output survives."""
    return logs_dir() / "app.log"

def config_path() -> Path:
    override = os.environ.get("YT_PLAYLIST_HOME")
    if override:
        return Path(override) / "config.toml"
    if _native_macos() and not os.environ.get("XDG_CONFIG_HOME"):
        d = _mac_support_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / "config.toml"
    d = _xdg_base("XDG_CONFIG_HOME", ".config") / "yt-playlist"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.toml"
