"""Capture and restore immutable listening context without depending on its consumers."""
from yt_playlist.core.evidence_ledger import canonical


def context_manifest(ledger, context):
    if context and 'artifact' in context:
        captured = ledger.get('artifact', context['artifact'])
        if captured is None:
            raise ValueError('Prepared listening context is no longer retained')
        return {**captured, **{key: value for key, value in context.items() if key != 'artifact'}}

    def share(value):
        if isinstance(value, dict):
            return {key: share(item) for key, item in value.items()}
        if isinstance(value, (list, str)) and len(canonical(value)) >= 65536:
            return {'$artifact': ledger.artifact(value)}
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
