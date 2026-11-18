"""Capture credential sources in precedence order, without exposing them to IAOs."""
import os
import tomllib

from yt_playlist.core import paths
from yt_playlist.core.config_files import ConfigFiles


def clean(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def precedence(environment=None, setting=None, configuration=None):
    """Detached credential policy over already acquired values."""
    return clean(environment) or clean(setting) or clean(configuration)


class AccessConfiguration:
    def __init__(self, settings=None, *, environment=None, files=None, config_path=None):
        self.settings = settings
        self.environment = os.environ if environment is None else environment
        self.files = ConfigFiles() if files is None else files
        self.config_path = config_path

    def credential(self, env_name, setting_name):
        value = precedence(self.environment.get(env_name))
        if value is not None:
            return value
        value = precedence(setting=self.settings.get_setting(setting_name)) if self.settings else None
        if value is not None:
            return value
        try:
            data = tomllib.loads(self.files.read(self.config_path or paths.config_path()).decode('utf-8'))
        except (OSError, ValueError):
            return None
        return precedence(configuration=data.get(setting_name))

    def credential_source(self, env_name, setting_name):
        """A live admission capability: re-read current sources after pacing."""
        return lambda: self.credential(env_name, setting_name)


def resolve(*, env_name, setting_name, store=None, config_path=None):
    return AccessConfiguration(getattr(store, 'settings', None), config_path=config_path).credential(
        env_name, setting_name)
