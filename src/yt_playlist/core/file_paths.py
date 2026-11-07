"""Locations chosen by file owners, independent of the file encoding."""
from pathlib import Path
import tempfile


def root_for_database(path, memory_key, domain):
    if path:
        path = Path(path).resolve()
        return path.with_name(path.name + '.' + domain)
    return Path(tempfile.gettempdir()) / ('tuneconsole-' + domain) / str(memory_key)


def directory(conn, domain):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, getattr(conn, '_policy_memory_key', id(conn)), domain)
