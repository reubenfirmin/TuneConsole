"""Retained file, inbox and construction lifetimes shared by application readers."""
import shutil
import uuid
from contextlib import contextmanager, ExitStack

from yt_playlist.core import (collection_recovery, evidence_ledger, file_owners, file_paths,
    group_documents, legacy_evidence, metadata_delivery_files, metadata_runtime_files,
    policy_documents, recommendation_outputs, served_facts)
from yt_playlist.library.playback_inbox_composition import inbox_for_database
from yt_playlist.library.store_composition import bind


class ApplicationLifecycle:
    bind = staticmethod(bind)

    @staticmethod
    def recover(path):
        if path is not None:
            from yt_playlist.core.db_rebuild.install import recover
            recover(path)

    @staticmethod
    def attach(session):
        session._evidence_key = uuid.uuid4().hex
        session._playback_feedback = inbox_for_database(session._db_path, session._evidence_key)
        session.conn._policy_memory_key = session._evidence_key
        session.transient_radio_playlists = set()
        if session._db_path is None:
            # Register all retained owners before deriving their private memory roots.
            roots = [metadata_runtime_files.root_for_database(None, session._evidence_key),
                     evidence_ledger.root_for_database(None, session._evidence_key)]
            roots.extend(file_paths.root_for_database(None, session._evidence_key, domain)
                         for domain in file_owners.owners())
            for root in roots:
                session.conn._close_callbacks.append(lambda root=root: shutil.rmtree(root, ignore_errors=True))

    @staticmethod
    def attach_reader(source, reader):
        reader._evidence_key = source._evidence_key
        reader.transient_radio_playlists = source.transient_radio_playlists
        reader.conn._policy_memory_key = source._evidence_key

    @staticmethod
    def repair(session):
        if not session._schema_compatibility:
            from yt_playlist.core import repair
            repair.run_once(session)

    @staticmethod
    @contextmanager
    def readers(session):
        """Pin every retained owner before opening or rebinding the SQL reader."""
        path, key = session._db_path, session._evidence_key
        owners = [policy_documents, evidence_ledger, served_facts]
        for flag, modules in (
            ('_collection_files', (collection_recovery,)),
            ('_compact_group_files', (group_documents,)),
            ('_recommendation_files', (recommendation_outputs,)),
            ('_metadata_files', (metadata_delivery_files, metadata_runtime_files)),
            ('_legacy_files', (legacy_evidence,)),
        ):
            if getattr(session, flag, False):
                owners.extend(modules)
        with ExitStack() as retained:
            retained.enter_context(file_owners.readers(path, key))
            for owner in owners:
                retained.enter_context(policy_documents.retained_reader(owner.root_for_database(path, key)))
            yield
