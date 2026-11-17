"""Compatibility entry points for immutable listening contexts."""
from yt_playlist.core.choice_contexts import component_ids, correction_context, resolve_context
from yt_playlist.core.choice_checkpoints import CORRECTION_CONTRACT, MAX_CORRECTION_BYTES, checkpoint
from yt_playlist.controllers.choice_context import ChoiceContexts


def learning_context(store, identity, *, ancestry=True):
    return ChoiceContexts(store.rec_ledger.evidence()).learning(identity)


def context_manifest(ledger, context, *, publish=None):
    return ChoiceContexts(ledger).manifest(context, publish=publish)
