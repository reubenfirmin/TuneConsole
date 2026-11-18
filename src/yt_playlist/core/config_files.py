"""Explicit configuration-file access; validation belongs to config/setup policy."""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class ConfigFiles:
    def read(self, path):
        return Path(path).read_bytes()

    def exists(self, path):
        return Path(path).exists()

    def mkdir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    @contextmanager
    def stage(self, path, content):
        path = Path(path)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', delete=False) as file:
                staged = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            yield staged
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def publish(self, staged, path):
        os.replace(staged, path)

    def restore(self, content, path):
        if content is None:
            Path(path).unlink(missing_ok=True)
        else:
            with self.stage(path, content) as staged:
                self.publish(staged, path)
