"""Finite FAO adoption inventory; schema, references and policy stay in each owner.

This is a code inventory, not a persisted file or artifact registry.
"""
from contextlib import contextmanager, ExitStack

from . import file_paths, policy_documents, profile_files, vibe_provenance, embedding_files, legacy_journals, policy_history
from yt_playlist.repos import vibe_models


def owners():
    return {'vibe-provenance': vibe_provenance, 'vibe-models': vibe_models, 'profiles': profile_files, 'embeddings': embedding_files,
            'legacy-journals': legacy_journals, 'policy-history': policy_history}


@contextmanager
def readers(path, memory_key):
    with ExitStack() as stack:
        for domain in owners():
            stack.enter_context(policy_documents.retained_reader(file_paths.root_for_database(path, memory_key, domain)))
        yield


def references(conn):
    for owner in owners().values():
        if owner.available(conn):
            yield from owner.references(conn)
