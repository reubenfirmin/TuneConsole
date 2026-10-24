"""Resolve provider credentials from environment, saved settings, then TOML."""
import os
import tomllib

from yt_playlist.core import paths


def resolve(*, env_name, setting_name, store=None, config_path=None):
    value = os.environ.get(env_name)
    if value and value.strip():
        return value.strip()
    if store is not None:
        value = store.settings.get_setting(setting_name)
        if value and value.strip():
            return value.strip()
    path = config_path or paths.config_path()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None
    value = data.get(setting_name)
    return value.strip() if isinstance(value, str) and value.strip() else None
