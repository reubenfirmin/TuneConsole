"""Custody of file owners whose root follows the database filename.

SQL-stored absolute references and installed relative roots remain in place
under the job's reader fences. Private database names borrow those roots.
The directory inventory comes from domain owners, not filesystem discovery.
"""
from yt_playlist.core import file_paths
from pathlib import Path
import os

from .. import (choice_evidence_files, evidence_ledger, fao, file_owners,
               metadata_runtime, metadata_runtime_files, served_facts)
from ..evidence_ledger import _sync_directory


def roots(database):
    database = Path(database)
    return {**{name: file_paths.root_for_database(database, None, name) for name in file_owners.owners()},
            'choice_evidence': evidence_ledger.root_for_database(database, None),
            'runtime_metadata': metadata_runtime_files.root_for_database(database, None),
            'served': served_facts.root_for_database(database, None)}


def owners(conn):
    for name, owner in file_owners.owners().items():
        if owner.available(conn):
            yield name, owner, owner.references(conn)
    yield 'choice_evidence', choice_evidence_files, choice_evidence_files.references(conn)
    if served_facts.available(conn):
        root = served_facts.directory(conn)
        yield 'served', served_facts, (root / served_facts.relative(row['id'])
                                      for row in served_facts.references(conn))
    if metadata_runtime.active(conn):
        root = metadata_runtime_files.directory(conn)
        yield 'runtime_metadata', metadata_runtime_files, (
            root / metadata_runtime_files.relative(kind, owner, revision)
            for kind, owner, revision, required in metadata_runtime_files.references(conn)
            if required or (root / metadata_runtime_files.relative(kind, owner, revision)).exists())


def borrow(source, candidate):
    """Let a private SQL candidate use the installed immutable file owners.

    Cutover keeps the installed database filename and its file directories.
    Only the private database names need links; existing files never move.
    """
    installed = roots(source)
    for name, destination in roots(candidate).items():
        root = installed[name].resolve()
        root.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            if destination.resolve() != root:
                raise ValueError('Candidate file owner points to another database: ' + name)
        elif destination.exists():
            raise ValueError('Candidate file owner already has independent custody: ' + name)
        else:
            destination.symlink_to(root, target_is_directory=True)


def _move(source, target):
    os.replace(source, target)
    _sync_directory(source.parent)
    _sync_directory(target.parent)


def install(state, *, progress):
    """Called only after the original SQL moved and every handle drained."""
    for item in state.get('file_roots', ()):
        installed, original, candidate = (Path(item[key]) for key in ('installed', 'original', 'candidate'))
        if item['had_original']:
            _move(installed, original)
        if item['has_candidate']:
            _move(candidate, installed)
        progress('installing_files', {'domain': item['domain']})


def rollback(state):
    """The durable plan and rename destinations make recovery repeatable."""
    for item in reversed(state.get('file_roots', ())):
        installed, original, candidate = (Path(item[key]) for key in ('installed', 'original', 'candidate'))
        if original.exists():
            if installed.exists():
                _move(installed, candidate)
            _move(original, installed)
        elif not item['had_original'] and installed.exists() and not candidate.exists():
            _move(installed, candidate)


def verify(conn):
    for _, _, references in owners(conn):
        for path in references:
            fao.read(path).digest
