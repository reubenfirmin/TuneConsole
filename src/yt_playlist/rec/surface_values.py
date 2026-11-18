"""Local recommendation ordering from supplied candidates, weights and scores."""
from collections import deque
from dataclasses import dataclass
import numpy as np
from yt_playlist.rec import vector_math


@dataclass
class ForYouItem:
    title: str
    artist: str
    album: str
    video_id: str | None
    thumbnail: str | None
    plays: int
    reason: str
    key: str = ''
    lane: str = ''
    genre: str = ''
    year: int | None = None
    trace: dict | None = None
    score: float | None = None


def neighborhood_keys(scores, seeds, limit):
    return [key for key in sorted(scores, key=lambda key: -scores[key]) if key not in seeds][:limit]


def _weighted_fair_queue(queues, limit, hard, muted, traces=None):
    for queue in queues:
        if not isinstance(queue[0], deque):
            queue[0] = deque(queue[0])
    seen, out = set(), []
    while len(out) < limit:
        live = [queue for queue in queues if queue[0]]
        if not live:
            break
        queue = max(live, key=lambda queue: queue[3] / (queue[4] + 1))
        rows, reason, lane, _, _ = queue
        row = rows.popleft()
        if row['key'] in seen or row['key'] in hard or row['artist'] in muted:
            seen.add(row['key'])
            continue
        seen.add(row['key'])
        queue[4] += 1
        out.append(ForYouItem(row['title'], row['artist'], row['album'], row['video_id'],
            row['thumbnail'], row['plays'], reason(row), row['key'], lane,
            trace=(traces or {}).get(row['key'])))
    return out


def wheelhouse_values(sources, sims, weights, hard, muted, limit, *, traces=None,
                      taste=None, vectors=None, index=None, include_traces=False):
    if sims is not None:
        sources = [(sorted(rows, key=lambda row: -sims.get(row['key'], -1.)), reason, lane)
                   for rows, reason, lane in sources]
    queues = [[deque(rows), reason, lane, weights.get(f'lane:{lane}', 1.), 0]
              for rows, reason, lane in sources]
    out = _weighted_fair_queue(queues, limit, hard, muted, traces)
    for item in out:
        item.score = sims.get(item.key) if sims is not None else None
    if include_traces and taste and vectors is not None:
        for item in out:
            if item.trace is None or item.key not in index:
                continue
            contributions = taste.context_contributions(vectors[index[item.key]])
            order = np.argsort(-contributions)[:3]
            item.trace['contexts'] = [{'name': taste.titles[i], 'contribution': float(contributions[i])}
                                      for i in order if contributions[i] > 0]
    return out


def comfort_values(rows, suppressed, muted, limit):
    out = []
    for row in rows:
        if len(out) >= limit:
            break
        if row['key'] in suppressed or row['artist'] in muted:
            continue
        out.append(ForYouItem(row['title'], row['artist'], row['album'], row['video_id'],
            row['thumbnail'], row['plays'], "A favorite you haven't reached for lately",
            row['key'], 'comfort', score=row['plays']))
    return out


def _rotation_reason(n):
    return f"Sits with your favorites in {n} of your playlist{'s' if n != 1 else ''}"


CATALOG_CONTENT_FRAC = .25


def _interleave(primary, secondary, secondary_frac):
    out, pi, si = [], 0, 0
    for pos in range(len(primary) + len(secondary)):
        want_secondary = (secondary_frac > 0 and si < len(secondary)
                          and (pi >= len(primary) or (pos + 1) * secondary_frac > si))
        if want_secondary:
            out.append(secondary[si])
            si += 1
        elif pi < len(primary):
            out.append(primary[pi])
            pi += 1
        else:
            out.append(secondary[si])
            si += 1
    return out


def _catalog_scores(fit, plays, all_keys=None):
    base = vector_math.percentile_scores(fit)
    floor = .5 / max(len(fit), 1)
    keys = all_keys if all_keys is not None else fit.keys()
    return {key: (1. / (1. + plays.get(key, 0))) * base.get(key, floor) for key in keys}


def catalog_order(collab, content, owned, plays):
    order = _interleave(sorted(collab, key=lambda key: -collab[key]),
                        sorted(content, key=lambda key: -content[key]), CATALOG_CONTENT_FRAC)
    missing = owned - collab.keys() - content.keys()
    order.extend(sorted(missing, key=lambda key: (plays.get(key, 0), key)))
    return order, missing


def catalog_eligible(order, suppressed, genres, genre_axes, weights):
    return [key for key in order if key not in suppressed
            and weights.get(f"genre:{genre_axes.get(genres.get(key), ('', None))[0]}", 1.) != 0]


def catalog_values(order, meta, plays, muted, collab, content, missing, limit):
    out = []
    for key in order:
        row = meta.get(key)
        if not row or row['artist'] in muted:
            continue
        count = plays.get(key, 0)
        reason = (("Never played, from your library" if key in missing else "Never played, sits near your taste")
                  if count == 0 else f'Barely played ({count}×), worth another spin')
        out.append(ForYouItem(row['title'], row['artist'], row['album'], row['video_id'], row['thumbnail'],
                              count, reason, key, 'explore', score=collab.get(key, content.get(key))))
        if len(out) >= limit:
            break
    return out
