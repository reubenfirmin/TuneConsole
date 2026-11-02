"""Custody of file owners whose root follows the database filename.

SQL-stored absolute references and installed relative roots remain in place
under the job's reader fences. Private database names borrow those roots.
The directory inventory comes from domain owners, not filesystem discovery.
"""
from pathlib import Path
import os
import shutil

from .. import (choice_evidence_files, evidence_ledger, fao, file_owners,
               metadata_runtime, metadata_runtime_files, served_facts)
from ..evidence_ledger import _sync_directory


def roots(database):
    database = Path(database)
    return {**{name: fao.root_for_database(database, None, name) for name in file_owners.owners()},
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


def restore(conn, manifests):
    """Give a verified SQL backup its independent relative-file custody."""
    for name, owner, _ in owners(conn):
        if name in manifests:
            owner.restore(conn, manifests[name])


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


def copy(conn, database, *, cancelled=lambda: False, progress=lambda *_: None):
    """Copy explicit references; a retry verifies and reuses completed files.

    Each managed copy has FAO's bounded streaming I/O and preserves file age.
    This work runs before candidate SQL transactions and checks cancellation
    between members. No conversion bookmark depends on an unpublished file.
    """
    destination = roots(database)
    for name, owner, references in owners(conn):
        root = owner.directory(conn)
        for source in references:
            if cancelled():
                raise InterruptedError('Database file copy interrupted')
            target = destination[name] / source.relative_to(root)
            if target.exists():
                if fao._hash(source) != fao._hash(target):
                    raise ValueError('Resumed database file differs from its source: ' + name)
            else:
                fao.copy(source, target)
            progress('files', {'domain': name})


def stage(source, target, *, move, cancelled=lambda: False):
    for name, root in roots(source).items():
        if not root.exists():
            continue
        destination = roots(target)[name]
        if move:
            os.replace(root, destination)
        else:
            def member(source, target):
                if cancelled():
                    raise InterruptedError('Database file staging interrupted')
                shutil.copy2(source, target)
                with Path(target).open('rb') as stream:
                    os.fsync(stream.fileno())
                return target
            shutil.copytree(root, destination, copy_function=member)
            for directory, _, _ in os.walk(destination, topdown=False):
                _sync_directory(Path(directory))
        _sync_directory(destination)
        _sync_directory(destination.parent)


def plan(database, original, candidate):
    installed, saved, prepared = roots(database), roots(original), roots(candidate)
    return [dict(domain=name, installed=str(root), original=str(saved[name]), candidate=str(prepared[name]),
                 had_original=root.exists(), has_candidate=prepared[name].exists())
            for name, root in installed.items() if root.exists() or prepared[name].exists()]


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
            fao._hash(path)
