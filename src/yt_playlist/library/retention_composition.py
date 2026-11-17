"""Resolve maintenance roots at composition, separate from file-port work."""
from yt_playlist.controllers.managed_file_retention import ManagedFileRetention
from yt_playlist.core import (file_paths, policy_documents, metadata_delivery_files, audio_annotation_files,
    album_description_files, sample_files, legacy_evidence, metadata_runtime_files)
from yt_playlist.repos.managed_file_retention import ManagedFileRetentionRepo


def collection_repository(conn):
    """Bind the historical explicit-connection cleanup entry point."""
    from yt_playlist.core.maintenance_context import MaintenanceDatabase
    from yt_playlist.core.priority import ServingLock, RequestPriority
    from yt_playlist.repos.collection_retention import CollectionRetentionRepo
    return CollectionRetentionRepo(MaintenanceDatabase(lambda: conn, ServingLock(RequestPriority()), None, lambda: False))


def collect_collections(conn):
    """Run explicit cleanup to completion, unless a reader still owns the batch."""
    from yt_playlist.controllers.collection_retention import CollectionCollector
    from yt_playlist.core.collection_recovery import directory
    collector = CollectionCollector(directory(conn))
    repository = collection_repository(conn)
    try:
        while not collector.files.complete or collector.files.batch is not None:
            collector.step(repository)
            if collector.files.batch is not None:
                break
        return collector.files.removed
    finally:
        collector.close()


def managed_retention(database, inbox, memory_key):
    path = database._db_path
    contracts = (
        (policy_documents.root_for_database(path, memory_key), 'policies'),
        (metadata_delivery_files.root_for_database(path, memory_key), 'metadata-deliveries'),
        (audio_annotation_files.root_for_database(path, memory_key), 'annotations'),
        (album_description_files.root_for_database(path, memory_key), 'descriptions'),
        (sample_files.root_for_database(path, memory_key), 'samples'),
        (legacy_evidence.root_for_database(path, memory_key), 'archives'),
        (metadata_runtime_files.root_for_database(path, memory_key), 'metadata-runtime'),
        (inbox.directory, 'playback-inbox'),
    )
    # One preparation parent covers every installed domain, including stages
    # no longer written by runtime code. This resolution performs no SQL or I/O.
    pending_roots = (tuple(root for root, _ in contracts) + tuple(
        file_paths.root_for_database(path, memory_key, domain) for domain in (
            'vibe-provenance', 'vibe-models', 'profiles', 'embeddings', 'legacy-journals',
            'policy-history', 'evidence', 'groups', 'collections', 'recommendations', 'served', 'preparation'))
        if path else ())
    return ManagedFileRetention(database, ManagedFileRetentionRepo(database, inbox.path), contracts, pending_roots)
