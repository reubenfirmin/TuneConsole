"""Prospective forecasts and follow-up measurements from supplied model facts."""
from dataclasses import dataclass, field
from yt_playlist.rec import embedding_training as training
from yt_playlist.rec.vector_math import rank_by_context

DAY = 86400


def forecast(baskets, previous, selected, context, *, weights=None):
    old_keys, old_vectors = training.build_vectors_from_baskets(
        baskets, dim=previous['dim'], method=previous['method'], weights=weights)
    if previous == selected:
        keys, vectors = old_keys, old_vectors
    else:
        keys, vectors = training.build_vectors_from_baskets(
            baskets, dim=selected['dim'], method=selected['method'], weights=weights)
    return forecast_vectors((old_keys, old_vectors), (keys, vectors), context)


def forecast_vectors(previous, selected, context):
    old_keys, old_vectors = previous
    keys, vectors = selected
    eligible = (set(old_keys) & set(keys)) - context
    if len(eligible) <= 20 or not (context & set(old_keys)) or not (context & set(keys)):
        return None
    return {'eligible': sorted(eligible), 'previous': sorted(rank_by_context(old_keys, old_vectors, context, eligible)),
            'selected': sorted(rank_by_context(keys, vectors, context, eligible))}


def followup_intervals(forecasts, end):
    return [(i, forecast['made_at'], stop) for i, forecast in enumerate(forecasts)
            if (stop := min(end, forecasts[i + 1]['made_at']) if i + 1 < len(forecasts) else end) > forecast['made_at']]


def followup_facts(forecasts, intervals, now):
    measurement = Followup()
    for i, stop, plays, prior, generated in intervals:
        measurement.observe(forecasts[i], stop, plays, prior, generated)
    return measurement.summary(len(forecasts), now)


@dataclass
class Followup:
    total: int = 0
    old_hits: int = 0
    new_hits: int = 0
    excluded: int = 0
    points: list = field(default_factory=list)
    seen: set = field(default_factory=set)

    def observe(self, forecast, stop, plays, prior, generated):
        held = (set(plays) & set(forecast['eligible'])) - self.seen - set(prior)
        self.excluded += len(held & set(generated))
        held -= set(generated)
        self.seen |= held
        self.total += len(held)
        self.old_hits += len(held & set(forecast['previous']))
        self.new_hits += len(held & set(forecast['selected']))
        if held:
            self.points.append({'at': stop, 'trials': self.total, 'previous_hits': self.old_hits,
                                'selected_hits': self.new_hits, 'previous_score': self.old_hits / self.total,
                                'score': self.new_hits / self.total})

    def summary(self, forecasts, now):
        return {'trials': self.total, 'previous_hits': self.old_hits, 'selected_hits': self.new_hits,
                'previous_score': self.old_hits / self.total if self.total else None,
                'score': self.new_hits / self.total if self.total else None, 'updated_at': now,
                'generated_excluded': self.excluded, 'points': self.points, 'forecasts': forecasts}
