"""One-time, read-only custody capture for the optional local image manager."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch


@contextmanager
def private_capture_sync():
    """Batch durability for an unpublished candidate, never a live/test library.

    The manager syncs the Btrfs filesystem after both containers finish and before
    publishing. A crash before then discards the entire candidate. Keep exporters'
    content checks and atomic replacements; only defer their individual fsyncs.
    """
    if not Path('/library').is_dir() or os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1':
        raise RuntimeError('Batched capture sync is restricted to the private preparation container')
    with patch.object(os, 'fsync', lambda _descriptor: None):
        yield


@contextmanager
def source_files(path, destination, report, *, strict=False):
    from yt_playlist.core import evidence_ledger, policy_documents

    original_budget = evidence_ledger.EvidenceLedger._budget
    original_reader = policy_documents.retained_reader
    evidence = path.with_name(path.name + '.evidence').resolve()

    def forbidden_write(_value):
        raise AssertionError('Read-only capture cannot update the source evidence budget')

    @contextmanager
    def budget(ledger):
        if ledger.directory.resolve() != evidence:
            with original_budget(ledger) as value:
                yield value
            return
        filename = ledger.directory / '.budget'
        if not filename.exists():
            assert not any(ledger.directory.glob('*/*/*.json')), 'Source evidence has no budget lock'
            yield 0, forbidden_write
            return
        with filename.open('rb') as stream:
            release = policy_documents._try_file_lock(stream, exclusive=True)
            if release is None:
                raise RuntimeError('Source evidence publication is busy; retry preparation')
            try:
                yield int(stream.read()), forbidden_write
            finally:
                release()

    @contextmanager
    def reader(root):
        root = Path(root).resolve()
        if root.is_relative_to(destination.resolve()):
            with original_reader(root):
                yield
            return
        lock = root / '.readers.lock'
        if not lock.exists():
            report.setdefault('input_domains_without_reader_lock', []).append(str(root))
            if strict and any(p.is_file() and not p.name.startswith('.') for p in root.rglob('*')):
                raise RuntimeError('Source files lack a lifetime fence: ' + str(root))
            yield
            return
        with lock.open('rb') as stream:
            release = policy_documents._try_file_lock(stream, exclusive=False)
            if release is None:
                raise RuntimeError('Source file lifetime admission is busy; retry preparation')
            try:
                yield
            finally:
                release()

    with patch.object(policy_documents, 'retained_reader', reader), \
            patch.object(evidence_ledger.EvidenceLedger, '_budget', budget):
        yield


def export(source_path, target, report):
    """Pin one SQL snapshot and its file lifetimes; export every finite owner."""
    from tests import file_custody_support as custody
    from yt_playlist.core.db_rebuild import job

    with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA query_only=ON')
        with source_files(source_path, target, report, strict=True), job._files(source):
            source.execute('BEGIN')
            try:
                report['schema'] = source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
                with closing(sqlite3.connect(target / 'state.db')) as copied:
                    source.backup(copied, pages=256)
                manifests = custody.export_files(source, target / 'state.db', backup_root=target / 'exports')
                logical = {name: str(Path('/library') / Path(filename).relative_to(target))
                           for name, filename in manifests.items()}
                (target / 'exports.json').write_text(json.dumps(logical) + '\n')
                report['file_domains'] = sorted(manifests)
                report['source_total_changes'] = source.total_changes
            finally:
                source.rollback()


def finish():
    """Run inside the preparation container, installing stable logical paths."""
    from tests import file_custody_support as custody
    from tests.prepared_fixture_support import coordinates
    from yt_playlist.core.db_rebuild.sqlite_checks import verify

    root = Path('/library')
    with closing(sqlite3.connect(root / 'state.db')) as connection:
        connection.row_factory = sqlite3.Row
        custody.restore_files(connection, json.loads((root / 'exports.json').read_text()))
        verify(connection)
        for table, column, value in coordinates(connection):
            if (table.strip('"'), column.strip('"')) == ('action_backups', 'filename'):
                continue  # Human export name, not a physical file coordinate.
            path = Path(value)
            if not path.is_absolute() or not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError(f'Unisolated library file coordinate: {table}.{column}: {value}')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute('PRAGMA journal_mode=DELETE')
    shutil.rmtree(root / 'exports')
    (root / 'exports.json').unlink()


def verify_tree(root):
    digest = hashlib.sha256()
    count = size = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Baseline contains a symlink: ' + str(path))
        if path.is_file():
            with path.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            digest.update(json.dumps([str(path.relative_to(root)), path.stat().st_size, checksum]).encode())
            count += 1
            size += path.stat().st_size
    return dict(files=count, bytes=size, sha256=digest.hexdigest())


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'finish'))
    parser.add_argument('source', nargs='?', type=Path)
    args = parser.parse_args()
    with private_capture_sync():
        if args.action == 'finish':
            finish()
        else:
            if args.source is None:
                parser.error('Export requires a source database')
            report = {}
            export(args.source.resolve(strict=True), Path('/library'), report)
            Path('/library/.capture.json').write_text(json.dumps(report) + '\n')
