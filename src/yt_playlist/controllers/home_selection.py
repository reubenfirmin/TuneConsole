"""Prepare selection criteria and genre distances before scratch SQL queries."""
from yt_playlist.rec.home_selection_policy import query
from yt_playlist.util.genre_map import family_distance


class HomeSelection:
    def __init__(self, repository):
        self.repository = repository
        self.prepared_sources = set()

    def add_candidates(self, rows):
        self.repository.add_candidates(rows)
        self.prepared_sources.clear()

    def _prepare(self, selected, **ordering):
        plan = query(selected, **ordering)
        sources = {value for value in (selected.near_family, ordering.get('family')) if value}
        for source in sources:
            if source in self.prepared_sources:
                continue
            while missing := self.repository.missing_distance_families(source, limit=128):
                self.repository.add_distances([(source, target, family_distance(source, target))
                                               for target in missing])
            self.prepared_sources.add(source)
        return plan

    def count(self, selected):
        return self.repository.count(self._prepare(selected))

    def considered(self, card, selected):
        self.repository.considered(card, self._prepare(selected))

    def ranked(self, selected, *, mode=None, ranker='rank', family=None, scale=1):
        return self.repository.ranked(self._prepare(
            selected, mode=mode, ranker=ranker, family=family, scale=scale))

    def mark_seen(self, selected, keys):
        self.repository.mark_seen(self._prepare(selected), keys)

    def add_ppr(self, rows):
        self.repository.add_ppr(rows)

    def has_negative_ordering(self):
        return self.repository.has_negative_ordering()

    def ppr_position(self, mode, key):
        return self.repository.ppr_position(mode, key)

    def considered_rows(self, card):
        return self.repository.considered_rows(card)
