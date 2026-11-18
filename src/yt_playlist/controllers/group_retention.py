"""Plan group retirement on a private reader, then guard its bounded SQL write."""
from yt_playlist.core import group_retention_policy
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.repos import group_publication


class GroupRetention:
    def __init__(self, repository, snapshots):
        self.repository, self.snapshots = repository, snapshots

    def step(self):
        with self.snapshots() as snapshot:
            selected = plan(snapshot.store.group_retention, batch=group_publication.BATCH_ROWS)
            if selected is None:
                return False
            snapshot.release_reader()
            return snapshot.publish(lambda: self.repository.apply(selected))


def closure(repository, selected_roots):
    """Capture only reachable facts; the rule owns cycle and budget handling."""
    graph = group_retention_policy.Closure(*repository.root_references(
        selected_roots, group_retention_policy.MAX_REFERENCES + 1))
    while (source := graph.next_alias()) is not None:
        check_cancellable_work()
        graph.include(source, repository.published_successors(source, group_retention_policy.MAX_REFERENCES + 1))
    return graph.finish(repository.alias_origins(graph.aliases), repository.lineage_origins(graph.edges))


def plan(repository, *, batch):
    state = repository.retention_state()
    if not state['complete']:
        return None
    roots = group_retention_policy.roots(recommendations=state['recommendations'], checkpoint=state['checkpoint'])
    if state['walk']:
        commands = repository.advance_walk(roots)
        if commands is not None:
            return {'walk': commands, 'revision': state['revision']}
        selected = repository.walk_expired(batch)
        return dict(selected, revision=state['revision']) if selected else None
    generations, required = closure(repository, roots)
    expired = group_retention_policy.expired_edges(
        repository.published_edges(len(required) + batch), required, batch)
    if expired:
        return {'edges': repository.edge_selections(expired, batch)}
    candidates = group_retention_policy.unreferenced_generations(
        repository.published_generations(len(generations) + batch), generations)
    for generation in candidates:
        if not repository.alias_selected_elsewhere(generation):
            return {'generation': generation}
    return None
