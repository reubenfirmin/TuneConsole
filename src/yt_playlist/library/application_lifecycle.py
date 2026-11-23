"""Retained file, inbox and construction lifetimes shared by application readers."""
import shutil
import uuid
from contextlib import contextmanager, ExitStack

from yt_playlist.core import (collection_recovery, evidence_ledger, file_owners, file_paths,
    group_documents, legacy_evidence, metadata_delivery_files, metadata_runtime_files,
    policy_documents, recommendation_outputs, served_facts)
from yt_playlist.library.playback_inbox_composition import inbox_for_database
from yt_playlist.library.store_composition import bind
from yt_playlist.rec.group_viz import PublishedGroupGraph


class ApplicationLifecycle:
    bind = staticmethod(bind)

    @staticmethod
    def attach(session):
        session._evidence_key = uuid.uuid4().hex
        session.conn._policy_memory_key = session._evidence_key
        session.transient_radio_playlists = set()
        session._published_group_graph = PublishedGroupGraph()
        if session._db_path is None:
            # Register all retained owners before deriving their private memory roots.
            roots = [metadata_runtime_files.root_for_database(None, session._evidence_key),
                     evidence_ledger.root_for_database(None, session._evidence_key),
                     file_paths.root_for_database(None, session._evidence_key, 'playback-feedback')]
            roots.extend(file_paths.root_for_database(None, session._evidence_key, domain)
                         for domain in file_owners.owners())
            for root in roots:
                session.conn._close_callbacks.append(lambda root=root: shutil.rmtree(root, ignore_errors=True))
        session._playback_feedback = inbox_for_database(session._db_path, session._evidence_key)

    @staticmethod
    def attach_reader(source, reader):
        reader._evidence_key = source._evidence_key
        reader.transient_radio_playlists = source.transient_radio_playlists
        reader.conn._policy_memory_key = source._evidence_key
        reader._prepared_mix_listening = getattr(source, '_prepared_mix_listening', None)
        reader._published_group_graph = source._published_group_graph

    @staticmethod
    def copy_memory_state(source, reader):
        reader.conn._wiki_cards = source.conn._wiki_cards.copy()

    @staticmethod
    def repair(session):
        from yt_playlist.core import repair
        repair.run_once(session)

    @staticmethod
    @contextmanager
    def readers(session):
        """Pin every retained owner before opening or rebinding the SQL reader."""
        path, key = session._db_path, session._evidence_key
        owners = (policy_documents, evidence_ledger, served_facts, collection_recovery, group_documents,
                  recommendation_outputs, metadata_delivery_files, metadata_runtime_files, legacy_evidence)
        with ExitStack() as retained:
            retained.enter_context(file_owners.readers(path, key))
            for owner in owners:
                retained.enter_context(policy_documents.retained_reader(owner.root_for_database(path, key)))
            yield
