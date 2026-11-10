"""Actual evidence, publication context and outgoing lineage retain group history.

An alias retains its first publication. Each reachable published lineage edge
retains its earliest publication, rather than every repeated selection of it.
Builds retain their parent context; their own unfinished generation need not
expand. The durable SQL walk implements this same closure in bounded prefixes.
"""
from .activity import WorkBudgetExceeded, check_cancellable_work

MAX_REFERENCES = 8192


def roots(*, recommendations, checkpoint):
    selected = [('head', True), ('evidence', True), ('build', False), ('build_parent', True)]
    if recommendations:
        selected.append(('recommendation', True))
    if checkpoint:
        selected.append(('checkpoint', True))
    return selected


def _bounded(values, label):
    if len(values) > MAX_REFERENCES:
        raise WorkBudgetExceeded(f'Musical group retention exceeds its {label} budget')
    return values


def closure(repository, selected_roots):
    generations, aliases = repository.root_references(selected_roots, MAX_REFERENCES + 1)
    generations, aliases = set(generations), set(aliases)
    _bounded(generations, 'generation')
    _bounded(aliases, 'evidence')
    pending, visited, edges = list(aliases), set(), set()
    while pending:
        check_cancellable_work()
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for target in repository.published_successors(source, MAX_REFERENCES + 1):
            edges.add((source, target))
            _bounded(edges, 'lineage')
            aliases.add(target)
            if target not in visited:
                pending.append(target)
        _bounded(aliases, 'alias')
    generations.update(repository.alias_origins(aliases))
    generations.update(repository.lineage_origins(edges))
    _bounded(generations, 'generation')
    return generations, edges


def plan(repository, *, batch):
    state = repository.retention_state()
    if not state['complete']:
        return None
    selected_roots = roots(recommendations=state['recommendations'], checkpoint=state['checkpoint'])
    if state['walk']:
        commands = repository.advance_walk(selected_roots)
        if commands is not None:
            return {'walk': commands, 'revision': state['revision']}
        selected = repository.walk_expired(batch)
        return dict(selected, revision=state['revision']) if selected else None
    generations, required = closure(repository, selected_roots)
    expired = [pair for pair in repository.published_edges(len(required) + batch) if pair not in required][:batch]
    if expired:
        return {'edges': repository.edge_selections(expired, batch)}
    for generation in repository.published_generations(len(generations) + batch):
        if generation not in generations and not repository.alias_selected_elsewhere(generation):
            return {'generation': generation}
    return None
