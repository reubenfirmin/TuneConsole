"""Capture an actual library using read-only source handles and private output."""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3

from tests import file_custody_support as custody
from yt_playlist.core import evidence_ledger, policy_documents
from yt_playlist.core.db_rebuild import job


def copy_readonly_library(path, destination, monkeypatch, report):
    from tests.library_image_fixtures import managed_database
    managed = managed_database(path, report)
    if managed is not None:
        return managed
    original_budget = evidence_ledger.EvidenceLedger._budget

    @contextmanager
    def read_only_budget(ledger):
        if ledger.directory.resolve() != path.with_name(path.name + '.evidence').resolve():
            with original_budget(ledger) as budget:
                yield budget
            return
        budget_path = ledger.directory / '.budget'
        if not budget_path.exists():
            assert not any(ledger.directory.glob('*/*/*.json')), 'Source evidence has no budget lock'
            yield 0, forbidden_budget_write
            return
        with budget_path.open('rb') as stream:
            release = policy_documents._try_file_lock(stream, exclusive=True)
            assert release is not None, 'Source evidence publication is currently busy'
            try:
                yield int(stream.read()), forbidden_budget_write
            finally:
                release()

    def forbidden_budget_write(_value):
        raise AssertionError('Read-only source capture cannot update its evidence budget')

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
        copied.row_factory = sqlite3.Row
        with monkeypatch.context() as capture:
            capture.setattr(policy_documents, 'retained_reader', read_only_fence)
            capture.setattr(evidence_ledger.EvidenceLedger, '_budget', read_only_budget)
            with job._files(source):
                source.execute('BEGIN')
                report['source_version'] = source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
                source.backup(copied, pages=128)
                manifests = custody.export_files(source, destination)
                evidence = path.with_name(path.name + '.evidence')
                if evidence.exists():
                    shutil.copytree(evidence, destination.with_name(destination.name + '.evidence'))
                source.rollback()
        with copied:
            custody.restore_files(copied, manifests)
    return destination
