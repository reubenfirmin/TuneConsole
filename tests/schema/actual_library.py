"""Capture an actual library using read-only source handles and private output."""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3

from yt_playlist.core import copy_compaction, policy_documents, policy_recovery


def copy_readonly_library(path, destination, monkeypatch, report):
    @contextmanager
    def read_only_fence(root):
        lock = Path(root) / '.readers.lock'
        if not lock.exists():
            report.setdefault('input_domains_without_reader_lock', []).append(str(root))
            yield
            return
        with lock.open('rb') as stream:
            release = policy_documents._try_file_lock(stream, exclusive=False)
            assert release is not None, 'Source lifetime admission is currently busy'
            try:
                yield
            finally:
                release()

    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source, \
            closing(sqlite3.connect(destination)) as copied:
        source.row_factory = sqlite3.Row
        with monkeypatch.context() as capture:
            capture.setattr(policy_documents, 'retained_reader', read_only_fence)
            with copy_compaction._files(source):
                source.execute('BEGIN')
                report['source_version'] = source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
                source.backup(copied, pages=128)
                manifests = policy_recovery._export_files(source, destination)
                evidence = path.with_name(path.name + '.evidence')
                if evidence.exists():
                    shutil.copytree(evidence, destination.with_name(destination.name + '.evidence'))
                source.rollback()
        with copied:
            policy_recovery._restore_files(copied, manifests)
