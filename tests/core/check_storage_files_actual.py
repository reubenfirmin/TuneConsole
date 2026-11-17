"""Opt-in installed-library reference compatibility through the image manager."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import choice_evidence_files, fao, file_map, file_owners
from yt_playlist.core.db_rebuild import files, references, retention_plan


def test_actual_library_reference_discovery_and_reads(monkeypatch):
    database = managed_database()
    if database is None:
        pytest.skip('Run through scripts/test-library.py for an isolated actual-library snapshot')
    report = {}
    managed_database(report=report)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        with files.readers(references.reader_roots(conn)):
            def unexpected(*args, **kwargs):
                pytest.fail('SQL reference discovery performed file work')
            with monkeypatch.context() as guard:
                guard.setattr(fao, 'read', unexpected)
                guard.setattr(file_map, 'pages', unexpected)
                guard.setattr(Path, 'glob', unexpected)
                descriptors = list(references.inventory(conn))
                owners = [(name, list(selected)) for name, _, selected in references.owners(conn)]
                schema = references.foreign_keys(conn)
            selected = {name: set(files.paths(rows)) for name, rows in owners}
            # Compare the detached protocol to installed recovery's established
            # references, including legacy map pages and evidence directories.
            for name, owner in file_owners.owners().items():
                if owner.available(conn):
                    assert selected[name] == set(owner.references(conn))
            assert selected['choice_evidence'] == set(choice_evidence_files.references(conn))
            retained = set(files.paths(descriptors))
            logical_bytes = 0
            for path in retained:
                size, _ = fao.read(path).digest
                logical_bytes += size
            assert retained
            report.update(files=len(retained), logical_bytes=logical_bytes,
                owners={name: len(paths) for name, paths in selected.items()},
                retention_tables=sorted(retention_plan.plan(schema, now=10**10).predicates))
    report_file('/tmp/storage-boundaries-actual.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
