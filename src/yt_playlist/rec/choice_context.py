"""Capture and restore immutable listening context without depending on its consumers."""
import hashlib

from yt_playlist.core.evidence_ledger import canonical

CORRECTION_CONTRACT = 'choice-correction-v1'
MAX_CORRECTION_BYTES = 256 * 1024


def correction_context(context):
    """Sufficient frozen inputs for both learners, independent of today's model.

    Opportunities retain their actual options, scores, predictions and assignments;
    responses retain source IDs, observation times and withdrawal targets. Neither
    learner uses the saved taste matrices, full policy or Radio candidate snapshot.
    Keep historical definitions so corrections and future group drift can be replayed.
    """
    result = {key: context[key] for key in ('at', 'group_generation') if key in context}
    for field, keys in (
        ('groups', ('mode_id', 'space', 'centroid', 'rep_keys')),
        ('musical_groups', ('group_id', 'version', 'parent_version')),
        ('musical_definitions', ('group_id', 'version', 'generation', 'created_at', 'parent_version')),
    ):
        if field in context:
            result[field] = [{key: group[key] for key in keys if key in group}
                             for group in context[field]]
    if len(canonical(result)) > MAX_CORRECTION_BYTES:
        raise ValueError('Historical group definitions exceed the correction context budget')
    return result


def checkpoint(ledger, identity):
    """Validate an acknowledged, self-contained correction capture before using it."""
    value = ledger.get('checkpoint', identity)
    if (not value or value.get('schema') != 1 or value.get('contract') != CORRECTION_CONTRACT
            or value.get('source_context') != identity or not value.get('watermark')
            or not isinstance(value.get('context'), dict)):
        return None
    encoded = canonical(value['context'])
    if (len(encoded) > MAX_CORRECTION_BYTES
            or hashlib.sha256(encoded).hexdigest() != value.get('digest')):
        return None
    return value


def learning_context(store, identity):
    saved = checkpoint(store.rec_ledger.evidence(), identity)
    if not saved:
        return resolve_context(store, {'artifact': identity})
    context = saved['context']
    definitions = []
    for frozen in context.get('musical_definitions', []):
        # Early v1 checkpoints omitted ancestry. Recover only that field from
        # the exact immutable definition, even after its raw context is released.
        # A mismatched or unavailable definition remains unknown.
        if 'parent_version' not in frozen:
            original = store.taste_groups.definition(frozen['version'])
            if original and all(original.get(key) == frozen.get(key)
                                for key in ('group_id', 'version', 'generation', 'created_at')):
                frozen = {**frozen, 'parent_version': original['parent_version']}
        definitions.append(frozen)
    return {**context, 'musical_definitions': definitions} if definitions else context


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
