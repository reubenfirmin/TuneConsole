"""Custody of file owners whose root follows the database filename.

SQL-stored absolute references and installed relative roots remain in place
under the job's reader fences. Private database names borrow those roots.
The directory inventory comes from domain owners, not filesystem discovery.
"""
from yt_playlist.core import file_paths
from contextlib import contextmanager, ExitStack
from pathlib import Path
import hashlib
import os

from .. import evidence_ledger, fao, file_owners, metadata_runtime_files, served_facts, policy_documents
from ..evidence_ledger import _sync_directory


def source_identity(path):
    """Fingerprint write-fenced SQL/WAL bytes for conservative restart admission."""
    result = {}
    for candidate in (path, path.with_name(path.name + '-wal')):
        try:
            with candidate.open('rb') as stream:
                if candidate != path and candidate.stat().st_size <= 32:
                    continue
                result[candidate.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        except FileNotFoundError:
            if candidate == path:
                raise
    return {'path': str(path), 'files': result}


def tree_bytes(path):
    count = size = allocated = 0
    for file in path.rglob('*'):
        if file.is_file():
            info = file.stat()
            count += 1
            size += info.st_size
            allocated += getattr(info, 'st_blocks', 0) * 512
    return dict(files=count, bytes=size, allocated_bytes=allocated)


def same_filesystem(left, right):
    return left.stat().st_dev == right.stat().st_dev


def roots(database):
    database = Path(database)
    return {**{name: file_paths.root_for_database(database, None, name) for name in file_owners.owners()},
            'choice_evidence': evidence_ledger.root_for_database(database, None),
            'runtime_metadata': metadata_runtime_files.root_for_database(database, None),
            'served': served_facts.root_for_database(database, None)}


def owners(conn):
    """Compatibility composition for installed recovery callers."""
    from . import references
    for name, owner, selected in references.owners(conn):
        yield name, owner, paths(selected)


def paths(references):
    from .references import MapReference, EvidenceReferences
    from .. import file_map
    from ..evidence_paths import record_id
    for reference in references:
        if isinstance(reference, MapReference):
            yield from file_map.pages(reference.root, reference.publication)
        elif isinstance(reference, EvidenceReferences):
            for kind in sorted(evidence_ledger.EvidenceLedger.KINDS):
                yield from (path for path in sorted((reference.root / kind).glob('*/*.json'))
                            if record_id(path) is None or record_id(path) in reference.owned)
        elif reference.required or reference.path.exists():
            yield reference.path


def inspect(references):
    # Deduplicate without retaining every stat result alongside the paths.
    count = size = allocated = 0
    for path in set(paths(references)):
        stat = path.stat()
        count += 1
        size += stat.st_size
        allocated += getattr(stat, 'st_blocks', 0) * 512
    return dict(files=count, bytes=size, allocated_bytes=allocated)


@contextmanager
def readers(roots):
    with ExitStack() as stack:
        for root in roots:
            stack.enter_context(policy_documents.retained_reader(root))
        yield


def verify_references(references):
    for path in paths(references):
        fao.read(path).digest


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
