"""Read user-supplied configuration files."""
from pathlib import Path


class ConfigFiles:
    def read(self, path):
        return Path(path).read_bytes()
