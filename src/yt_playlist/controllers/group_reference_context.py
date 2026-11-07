"""Resolve declared group-reference fields through the evidence file access object."""
from yt_playlist.core import choice_checkpoints, group_evidence, recommendation_references


def resolve(captured, ledger, *, recommendations):
    if not isinstance(captured, dict):
        raise ValueError('Invalid listening context for musical group evidence')
    if captured.get('artifact'):
        original = ledger.get('artifact', captured['artifact'])
        if original is None:
            saved = choice_checkpoints.checkpoint(ledger, captured['artifact'])
            original = saved['context'] if saved else None
        if not isinstance(original, dict):
            raise ValueError('Listening group context is no longer retained')
        captured = {**original, **{key: value for key, value in captured.items() if key != 'artifact'}}
    result = {'group_generation': captured['group_generation']} if 'group_generation' in captured else {}
    for field in ('groups', 'musical_groups', 'musical_definitions'):
        if field not in captured:
            continue
        values = captured[field]
        if isinstance(values, dict) and set(values) == {'$artifact'}:
            values = ledger.get('artifact', values['$artifact'])
        if not isinstance(values, list):
            raise ValueError('Listening group definitions are no longer retained')
        result[field] = [{key: value[key] for key in ('version', 'definition_version', 'group_id') if key in value}
                         if isinstance(value, dict) else value for value in values]
    group_evidence.references(result)
    if recommendations:
        result['recommendation_generations'] = recommendation_references.references(captured)
    return result
