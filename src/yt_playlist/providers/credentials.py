"""Resolve provider credentials from environment, saved settings, then TOML."""
import os
import tomllib

from yt_playlist.core import paths


def _clean(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _env_source(env_name):
    return _clean(os.environ.get(env_name))


def _setting_source(store, setting_name):
    return None if store is None else _clean(store.settings.get_setting(setting_name))


def _config_file_source(setting_name, config_path):
    path = config_path or paths.config_path()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return _clean(data.get(setting_name))


def resolve(*, env_name, setting_name, store=None, config_path=None):
    # Each source is read only once the higher-precedence ones come up empty;
    # the config file in particular is never opened when a setting is enough.
    env_value = _env_source(env_name)
    if env_value is not None:
        return env_value
    setting_value = _setting_source(store, setting_name)
    if setting_value is not None:
        return setting_value
    return _config_file_source(setting_name, config_path)
