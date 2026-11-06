"""Home filtering and ordering expressed as bounded database query inputs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateFilter:
    pool: int | None
    year_band: int | None = None
    years: tuple = ()
    include: tuple = ()
    avoid: tuple = ()
    tagged: bool = False
    eligible: bool = False
    unseen: bool = False
    excluded: tuple = ()
    near_family: str | None = None


@dataclass(frozen=True)
class CandidateQuery:
    predicate: str
    values: tuple
    order: str = 'c.rank'
    order_values: tuple = ()
    mode: str | None = None


def query(selected, *, mode=None, ranker='rank', family=None, scale=1):
    parts, values = ['c.pool=?'], [selected.pool]
    if selected.year_band is not None:
        low, high = selected.years
        parts.append('c.year<=?' if selected.year_band == 0 else
                     'c.year>? AND c.year<=?' if selected.year_band == 1 else 'c.year>?')
        values.extend((low,) if selected.year_band == 0 else
                      (low, high) if selected.year_band == 1 else (high,))
    if selected.tagged:
        parts.append('c.has_genre=1')
    for items, expression in ((selected.include, 'c.family IN'), (selected.avoid, 'c.family NOT IN'),
                              (selected.excluded, 'c.key NOT IN')):
        if items:
            parts.append(expression + ' (' + ','.join('?' for _ in items) + ')')
            values.extend(items)
    if selected.near_family:
        parts.append("EXISTS(SELECT 1 FROM distances d WHERE d.source=? AND d.target=COALESCE(c.family,'') AND d.distance<=?)")
        values.extend((selected.near_family, .5))
    if selected.eligible:
        parts.append('c.eligible=1')
    if selected.unseen:
        parts.append('NOT EXISTS(SELECT 1 FROM seen s WHERE s.key=c.key)')
    order, parameters = 'c.rank', []
    if ranker != 'rank':
        if ranker not in ('ppr', 'cosine'):
            raise ValueError('Unknown Home ordering')
        order = 'COALESCE(p.rank,1073741824)/? - c.delta' if ranker == 'ppr' else '-c.tilt-c.delta'
        if family:
            order = "(SELECT d.distance FROM distances d WHERE d.source=? AND d.target=COALESCE(c.family,''))," + order
            parameters.append(family)
        if ranker == 'ppr':
            parameters.append(float(scale))
        order += ',c.rank'
    return CandidateQuery(' AND '.join(parts), tuple(values), order, tuple(parameters),
                          str(mode) if ranker != 'rank' else None)
