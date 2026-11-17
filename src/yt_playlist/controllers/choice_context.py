"""Acquire immutable context components before detached restoration."""
from yt_playlist.core.choice_checkpoints import checkpoint
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.choice_contexts import component_ids, correction_context, resolve_context


class ChoiceContexts:
    def __init__(self, ledger):
        self.ledger = ledger

    def learning(self, identity):
        saved = checkpoint(self.ledger, identity)
        return correction_context(saved['context']) if saved else self.resolve({'artifact': identity})

    def manifest(self, context, *, publish=None):
        if context and 'artifact' in context:
            captured = self.ledger.get('artifact', context['artifact'])
            if captured is None:
                raise ValueError('Prepared listening context is no longer retained')
            return {**captured, **{key: value for key, value in context.items() if key != 'artifact'}}
        publish = self.ledger.artifact if publish is None else publish

        def share(value):
            if isinstance(value, dict):
                return {key: share(item) for key, item in value.items()}
            if isinstance(value, (list, str)) and len(canonical(value)) >= 65536:
                return {'$artifact': publish(value)}
            return value
        return share(context)

    def resolve(self, context):
        manifest = self.manifest(context) if context and 'artifact' in context else context
        components = {identity: self.ledger.get('artifact', identity) for identity in component_ids(manifest)}
        return resolve_context(manifest, components)
