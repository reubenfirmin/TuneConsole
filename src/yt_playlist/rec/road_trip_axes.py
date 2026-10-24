"""Road Trip axes and integer percentage budgets, independent of draft jobs and acquisition.

Functions accept only track snapshots, displayed axes and requested targets. They never read a
library or call a provider. Actual track shares remain separate from requested slider positions.
"""
from collections import Counter

from yt_playlist.providers import genres as genre_names
from yt_playlist.rec.road_trip_mix import AVG_TRACK_S, _apportion_percentages, _axis_genre
from yt_playlist.util import genre_map

GENRE_BARS = 8


def canonical_genre(genre):
    genre = (genre or "").strip()
    return (genre_names.match_tag(genre) or genre.title()) if genre else ""


def matches(candidate, axis):
    kind, name = axis.split(":", 1)
    return _axis_genre(candidate) == name if kind == "genre" else candidate["decade"] == name


def available_seconds(pool, axis):
    """Passenger pool capacity; the live library is never a bounded passenger pool."""
    return sum((c["duration"] or AVG_TRACK_S) for c in pool
               if c["source"] != "mine" and matches(c, axis))


def widen_terms(pool, genres, axis):
    """Search terms for a genre, or an era crossed with the genres already in play."""
    kind, name = axis.split(":", 1)
    if kind == "genre":
        return list(genre_map.subgenres_of(name))[:3] or [name]
    names = list(genres) + [_axis_genre(c) for c in pool if c["source"] == "theirs"]
    seen, terms = set(), []
    for genre in names:
        if genre and genre not in seen:
            seen.add(genre)
            terms.append(f"{genre} {name}s")
    return terms[:3] or [f"{name}s music"]


def order(axes):
    """Keep genre sliders in their sticky order, followed by decades on a number line."""
    return ([a for a in axes if a.get("kind") != "era"]
            + sorted((a for a in axes if a.get("kind") == "era"), key=lambda a: a.get("name") or ""))


def merge(previous, picked, available=(), max_genres=GENRE_BARS):
    """Refresh observed shares, retaining pinned or still available sliders at zero.

    Only newly encountered genres count against the display limit. Existing controls retain their
    position while the pool can fill them; pinned controls survive even an exhausted pool.
    """
    total = len(picked) or 1
    counts = Counter(_axis_genre(c) for c in picked if _axis_genre(c))
    eras = Counter(c["decade"] for c in picked if c["decade"])
    shares = {("genre:" + name): count / total for name, count in counts.items()}
    shares.update({("era:" + name): count / total for name, count in eras.items()})
    live = {"genre:" + _axis_genre(c) for c in available if _axis_genre(c)}
    live |= {"era:" + c["decade"] for c in available if c["decade"]}
    out, seen = [], set()
    for axis in previous or []:
        if axis["key"] not in live and axis.get("target") is None:
            continue
        seen.add(axis["key"])
        out.append({**axis, "share": round(shares.get(axis["key"], 0.0), 3)})
    room = max(0, max_genres - sum(a["kind"] == "genre" for a in out))
    genres = sorted(((key, share) for key, share in shares.items()
                     if key not in seen and key.startswith("genre:")), key=lambda item: -item[1])[:room]
    eras = sorted((key, share) for key, share in shares.items()
                  if key not in seen and key.startswith("era:"))
    for key, share in genres + eras:
        kind, name = key.split(":", 1)
        out.append({"key": key, "kind": kind, "name": name, "share": round(share, 3)})
    return order(out)


def normalize(axes, targets):
    """Normalize one party's axes and targets in place; return its ordered axes.

    Keep valid pins, give unpinned rows the remaining budget and scale oversubscribed legacy pins
    together. Genre and era groups have independent budgets. A lone pinned row may remain partial.
    """
    for kind in ("genre", "era"):
        rows = [a for a in axes if a["kind"] == kind]
        keys = {a["key"] for a in rows}
        for key in targets:
            if key.startswith(kind + ":") and key not in keys:
                row = {"key": key, "kind": kind, "name": key.split(":", 1)[1], "share": 0.0}
                axes.append(row)
                rows.append(row)
        if not rows:
            continue
        pins = {a["key"]: targets[a["key"]] for a in rows if a["key"] in targets}
        floating = {a["key"]: a.get("share", 0.0) for a in rows if a["key"] not in pins}
        if pins:
            if len(rows) == 1:
                amounts = {rows[0]["key"]: round(next(iter(pins.values())) * 100)}
            elif sum(pins.values()) >= 1 or not floating:
                amounts = {key: 0 for key in floating}
                amounts.update(_apportion_percentages(pins))
            else:
                claimed = round(sum(pins.values()) * 100)
                amounts = _apportion_percentages(pins, claimed)
                amounts.update(_apportion_percentages(floating, 100 - claimed))
            targets.update({key: amounts[key] / 100 for key in pins})
        else:
            amounts = _apportion_percentages(floating)
        for row in rows:
            row["balance_pct"] = amounts[row["key"]]
            row["target"] = targets.get(row["key"])
    return order(axes)


def add_genres(axes, targets, genres):
    """Reserve equal shares for additions, scaling existing passenger genre shares together."""
    axes[:] = normalize(axes, targets)
    existing = {a["key"]: a["balance_pct"] for a in axes if a["kind"] == "genre"}
    added = {"genre:" + name: name for genre in genres if (name := canonical_genre(genre))
             and "genre:" + name not in existing}
    if not added:
        return
    reserved = round(100 * len(added) / (len(existing) + len(added)))
    amounts = _apportion_percentages(existing, 100 - reserved)
    amounts.update(_apportion_percentages({key: 1 for key in added}, reserved))
    targets.update({key: pct / 100 for key, pct in amounts.items()})
    axes.extend({"key": key, "kind": "genre", "name": name, "share": 0.0,
                 "target": amounts[key] / 100} for key, name in added.items())


def rebalance(axes, targets, axis, share):
    """Keep the edited percentage; apportion its siblings' remainder in whole percentages."""
    kind, name = axis.split(":", 1)
    if not any(a["key"] == axis for a in axes):
        axes.append({"key": axis, "kind": kind, "name": name, "share": 0.0, "target": share})
    siblings = {a["key"]: targets.get(a["key"], a.get("balance_pct", a.get("share", 0.0) * 100) / 100)
                for a in axes if a["kind"] == kind and a["key"] != axis}
    siblings.update({key: value for key, value in targets.items()
                     if key.startswith(kind + ":") and key != axis})
    pct = round(share * 100)
    targets[axis] = pct / 100
    if siblings:
        targets.update({key: value / 100 for key, value in
                        _apportion_percentages(siblings, 100 - pct).items()})
