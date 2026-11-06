"""Finite FAO adoption inventory; schema, references and policy stay in each owner.

This is a code inventory, not a persisted file or artifact registry.
"""
from contextlib import contextmanager, ExitStack

from . import fao, policy_documents, profile_files, vibe_provenance, embedding_files, legacy_journals, policy_history
from yt_playlist.repos import vibe_models


def owners():
    return {'vibe-provenance': vibe_provenance, 'vibe-models': vibe_models, 'profiles': profile_files, 'embeddings': embedding_files,
            'legacy-journals': legacy_journals, 'policy-history': policy_history}


@contextmanager
def readers(path, memory_key):
    with ExitStack() as stack:
        for domain in owners():
            stack.enter_context(policy_documents.retained_reader(fao.root_for_database(path, memory_key, domain)))
        yield


def export(conn, root):
    return {name: owner.export(conn, root(name)) for name, owner in owners().items() if owner.available(conn)}


def restore(conn, manifests):
    for name, owner in owners().items():
        if name in manifests:
            owner.restore(conn, manifests[name])


def references(conn):
    for owner in owners().values():
        if owner.available(conn):
            yield from owner.references(conn)
