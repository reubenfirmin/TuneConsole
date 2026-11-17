"""Capture and restore immutable listening context without depending on its consumers."""
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.choice_checkpoints import CORRECTION_CONTRACT, MAX_CORRECTION_BYTES, checkpoint


def correction_context(context):
    """Correction timing; offered descriptors and decision receipts live in menus."""
    result = {key: context[key] for key in ('at', 'precision') if key in context}
    if len(canonical(result)) > MAX_CORRECTION_BYTES:
        raise ValueError('Choice timing exceeds the correction context budget')
    return result


def component_ids(context):
    identities = set()
    def collect(value):
        if isinstance(value, dict):
            if set(value) == {'$artifact'}:
                identities.add(value['$artifact'])
            else:
                for item in value.values():
                    collect(item)
    collect(context)
    return identities


def resolve_context(context, components):
    """Restore only references in the original manifest, from verified captured bytes."""

    def restore(value):
        if isinstance(value, dict):
            if set(value) == {'$artifact'}:
                captured = components.get(value['$artifact'])
                if captured is None:
                    raise ValueError('Listening model component is no longer retained')
                return captured
            return {key: restore(item) for key, item in value.items()}
        return value
    return restore(context)
