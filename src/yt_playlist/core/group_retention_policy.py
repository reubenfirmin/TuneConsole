"""Actual evidence, publication context and outgoing lineage retain group history.

An alias retains its first publication. Each reachable published lineage edge
retains its earliest publication, rather than every repeated selection of it.
Builds retain their parent context; their own unfinished generation need not
expand. The durable SQL walk implements this same closure in bounded prefixes.
"""
from dataclasses import dataclass, field

from .activity import WorkBudgetExceeded

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


@dataclass
class Closure:
    """A bounded reachability calculation fed detached publication facts."""
    generations: set
    aliases: set
    edges: set = field(default_factory=set)
    visited: set = field(default_factory=set)
    pending: list = field(init=False)

    def __post_init__(self):
        self.generations = _bounded(set(self.generations), 'generation')
        self.aliases = _bounded(set(self.aliases), 'evidence')
        self.pending = list(self.aliases)

    def next_alias(self):
        while self.pending:
            source = self.pending.pop()
            if source not in self.visited:
                self.visited.add(source)
                return source
        return None

    def include(self, source, successors):
        for target in successors:
            self.edges.add((source, target))
            _bounded(self.edges, 'lineage')
            self.aliases.add(target)
            if target not in self.visited:
                self.pending.append(target)
        _bounded(self.aliases, 'alias')

    def finish(self, alias_origins, lineage_origins):
        self.generations.update(alias_origins)
        self.generations.update(lineage_origins)
        return _bounded(self.generations, 'generation'), self.edges


def expired_edges(published, required, batch):
    return [pair for pair in published if pair not in required][:batch]


def unreferenced_generations(published, retained):
    return [generation for generation in published if generation not in retained]
