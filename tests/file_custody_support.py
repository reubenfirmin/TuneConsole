"""Independent disposable library fixtures using the current domain exporters.

No schema upgrade, retention policy, compaction or installation runs here.
Production rebuilds keep installed file roots in place instead.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from yt_playlist.core import (action_backup_files, album_description_files, audio_annotation_files,
    choice_evidence_files, collection_recovery, file_owners, group_documents,
    legacy_evidence, metadata_delivery_files, metadata_destination_files, metadata_runtime,
    metadata_runtime_files, policy_documents, preparation_files, recommendation_outputs,
    sample_files, saved_preparation_files, served_facts)
from yt_playlist.core.db_rebuild import job
from yt_playlist.core.db_rebuild.sqlite_checks import tables, verify


def domains(conn):
    schema = tables(conn)
    yield 'choice_evidence', choice_evidence_files
    if served_facts.available(conn):
        yield 'served', served_facts
    if metadata_runtime.active(conn):
        yield 'runtime_metadata', metadata_runtime_files
    for table, name, module in (
        ('sample_attributes', 'samples', sample_files),
        ('action_backups', 'actions', action_backup_files),
        ('album_feedback', 'collections', collection_recovery),
        ('audio_annotations', 'annotations', audio_annotation_files),
        ('recommendation_policies', 'policies', policy_documents),
        ('album_description_requests', 'descriptions', album_description_files),
        ('group_versions', 'groups', group_documents),
        ('prepared_outputs', 'recommendations', recommendation_outputs),
        ('metadata_deliveries', 'metadata', metadata_delivery_files),
        ('published_metadata_destination_deliveries', 'metadata_preparation', metadata_destination_files),
    ):
        if table in schema:
            yield name, module
    if saved_preparation_files.available(conn):
        yield 'saved_preparation', saved_preparation_files
    if preparation_files.available(conn):
        yield 'preparation', preparation_files
    if 'legacy_catalog_evidence' in schema:
        yield 'archives', legacy_evidence


def export_files(conn, target, *, backup_root=None):
    target = Path(target)
    def root(domain):
        return (Path(backup_root) / domain if backup_root is not None else
                target.with_name(target.name + '.' + domain))
    manifests = file_owners.export(conn, root)
    for name, module in domains(conn):
        manifests[name] = module.export(conn, root(name))
    return manifests


def restore_files(conn, manifests):
    modules = dict(domains(conn))
    unknown = set(manifests) - set(file_owners.owners()) - set(modules)
    if unknown:
        raise ValueError('Unsupported fixture file domain: ' + repr(sorted(unknown)))
    file_owners.restore(conn, manifests)
    for name, module in modules.items():
        if name in manifests:
            module.restore(conn, manifests[name])


def snapshot_library(source, root, *, restore=True):
    """Export an exact snapshot; optionally restore it into a second library."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    snapshot, restored = root / 'snapshot.db', root / 'restored.db'
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        with job._files(reader):
            reader.execute('BEGIN')
            try:
                manifests = export_files(reader, snapshot)
                with closing(sqlite3.connect(snapshot)) as copied:
                    reader.backup(copied)
                    verify(copied)
            finally:
                reader.rollback()
    if restore:
        with closing(sqlite3.connect(snapshot)) as reader, closing(sqlite3.connect(restored)) as target:
            reader.backup(target)
            target.row_factory = sqlite3.Row
            target.execute('PRAGMA foreign_keys=ON')
            restore_files(target, manifests)
            verify(target)
    return {'snapshot': str(snapshot), 'restored': str(restored) if restore else None,
            'manifests': manifests}
