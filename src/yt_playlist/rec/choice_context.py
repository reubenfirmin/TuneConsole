"""Capture and restore immutable listening context without depending on its consumers."""
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.choice_checkpoints import CORRECTION_CONTRACT, MAX_CORRECTION_BYTES, checkpoint


def correction_context(context):
    """Correction timing; offered descriptors and decision receipts live in menus."""
    result = {key: context[key] for key in ('at', 'precision') if key in context}
    if len(canonical(result)) > MAX_CORRECTION_BYTES:
        raise ValueError('Choice timing exceeds the correction context budget')
    return result


def learning_context(store, identity, *, ancestry=True):
    saved = checkpoint(store.rec_ledger.evidence(), identity)
    if not saved:
        return resolve_context(store, {'artifact': identity})
    context = correction_context(saved['context'])
    return context


def context_manifest(ledger, context, *, publish=None):
    if context and 'artifact' in context:
        captured = ledger.get('artifact', context['artifact'])
        if captured is None:
            raise ValueError('Prepared listening context is no longer retained')
        return {**captured, **{key: value for key, value in context.items() if key != 'artifact'}}

    publish = ledger.artifact if publish is None else publish

    def share(value):
        if isinstance(value, dict):
            return {key: share(item) for key, item in value.items()}
        if isinstance(value, (list, str)) and len(canonical(value)) >= 65536:
            return {'$artifact': publish(value)}
        return value
    return share(context)


def resolve_context(store, context):
    ledger = store.rec_ledger.evidence()
    manifest = context_manifest(ledger, context) if context and 'artifact' in context else context

    def restore(value):
        if isinstance(value, dict):
            if set(value) == {'$artifact'}:
                captured = ledger.get('artifact', value['$artifact'])
                if captured is None:
                    raise ValueError('Listening model component is no longer retained')
                return captured
            return {key: restore(item) for key, item in value.items()}
        return value
    return restore(manifest)
