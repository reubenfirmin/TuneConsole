"""Pure road-trip allocation and ordering; no repositories, providers or draft writes."""
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Protocol

from yt_playlist.rec.journeys import journey_order
from yt_playlist.rec.artist_variety import members
from yt_playlist.rec.road_trip_artists import selected_artist
from yt_playlist.rec.road_trip_ranking import CROSSOVER_SIMILARITY, TrackRanking, sample_order
from yt_playlist.util import genre_map

AVG_TRACK_S = 210


class RandomSource(Protocol):
    def random(self) -> float: ...


@dataclass(frozen=True)
class MixRequest:
    candidates: list[dict]
    own_pct: int
    familiarity_pct: int
    target_minutes: int
    seed: int
    artists: list[str]
    genres: list[str]
    targets: dict[str, dict[str, float]]
    banned: list[str]
    previous: list[str]
    building: bool
    genre_families: dict[str,str] | None = None
    ranking: TrackRanking = field(default_factory=TrackRanking)
    artist_refs: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class MixResult:
    ordered: list[dict]
    picked: dict[str, list[dict]]
    groups: dict[str, list[dict]]
    short: dict[str, int]
    blend_available: bool


def _artist_cap(cands, needed):
    artists = {artist for candidate in cands for artist in members(candidate)}
    return max(2, math.ceil(needed / max(1, len(artists))) + 1)


def _axis_genre(cand):
    return cand.get("cluster_genre") or cand["genre"] or cand["family"] or ""


def _bucket(cand, kind, quota):
    key = ("genre:" + _axis_genre(cand)) if kind == "genre" else ("era:" + cand["decade"])
    return key if key in quota else ""


def _prefer_seed_artists(order, artists, refs):
    groups = {}
    for candidate in order:
        groups.setdefault(_axis_genre(candidate), []).append(candidate)
    queues = {}
    for genre, rows in groups.items():
        named, related = {}, []
        for candidate in rows:
            if artist := selected_artist(candidate, artists, refs):
                named.setdefault(artist, []).append(candidate)
            else:
                related.append(candidate)
        seeds = [c for group in zip_longest(*named.values()) for c in group if c]
        queues[genre] = deque(c for group in zip_longest(seeds[::2], seeds[1::2], related) for c in group if c)
    return [queues[_axis_genre(c)].popleft() for c in order]


def _quota_weights(state, party):
    targets = (state.get("targets") or {}).get(party) or {}
    out = {}
    for kind in ("genre", "era"):
        pins = {k: v for k, v in targets.items() if k.startswith(kind + ":")}
        if not pins:
            continue
        claimed = sum(pins.values())
        if claimed > 1.0:
            pins = {k: v / claimed for k, v in pins.items()}
        out[kind] = {**pins, "": max(0, 1 - claimed)}
    return out


def _apportion_percentages(weights, total=100):
    if not weights:
        return {}
    weight_sum = sum(weights.values())
    raw = {key: total * (value / weight_sum if weight_sum else 1 / len(weights)) for key, value in weights.items()}
    allocated = {key: math.floor(value) for key, value in raw.items()}
    remaining = total - sum(allocated.values())
    for key in sorted(raw, key=lambda key: -(raw[key] - allocated[key]))[:remaining]:
        allocated[key] += 1
    return allocated


def _quotas(weights, slots):
    return {kind: _apportion_percentages(shares, slots) for kind, shares in weights.items()}


def missed_balances(tracks, quotas):
    return [kind for kind, quota in quotas.items()
            if Counter(_bucket(candidate, kind, quota) for candidate in tracks) != Counter(quota)]


def _fill(order, slots, cap, quotas=None, preferred_artists=(), artist_refs=None, *, bucket_for=_bucket):
    quotas = quotas or {}
    picked, per, taken = [], Counter(), set()
    spent = {kind: Counter() for kind in quotas}
    total = 0.0

    def blocked(candidate, *, relax_artist=False):
        if len(picked) >= slots:
            return True
        if (not relax_artist and not selected_artist(candidate, preferred_artists, artist_refs or {})
                and any(per[artist] >= cap for artist in members(candidate))):
            return True
        for kind, quota in quotas.items():
            bucket = bucket_for(candidate, kind, quota)
            if spent[kind][bucket] >= quota.get(bucket, 0):
                return True
        return False

    def take(candidate):
        nonlocal total
        for kind, quota in quotas.items():
            spent[kind][bucket_for(candidate, kind, quota)] += 1
        per.update(members(candidate))
        taken.add(candidate["video_id"])
        picked.append(candidate)
        total += candidate["duration"] or AVG_TRACK_S

    for kind, quota in quotas.items():
        for key, want in quota.items():
            if not key:
                continue
            for candidate in order:
                if spent[kind][key] >= want:
                    break
                if candidate["video_id"] in taken or bucket_for(candidate, kind, quota) != key or blocked(candidate):
                    continue
                take(candidate)
    for candidate in order:
        if candidate["video_id"] not in taken and not blocked(candidate):
            take(candidate)
    # Keep explicit genre/era quotas; artist variety alone cannot shorten a mix.
    for candidate in order:
        if candidate['video_id'] not in taken and not blocked(candidate, relax_artist=True):
            take(candidate)
    return picked, total


def _joint_quotas(orders, wanted, weights, quotas):
    """Find track counts for the available intersections of source, genre and era."""
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import csc_array

    def cell(candidate):
        return tuple(_bucket(candidate, kind, quota) for kind, quota in quotas.items())

    limits = {('source', side): count for side, count in wanted.items()}
    limits.update({(kind, key): count for kind, quota in quotas.items() for key, count in quota.items()})
    columns = {}
    ranks = {}
    row_numbers = {key: index for index, key in enumerate(limits)}
    for side, order in orders.items():
        if not wanted[side]:
            continue
        for index, candidate in enumerate(order):
            keys = cell(candidate)
            if any(not shares[key] for key, shares in zip(keys, weights.values(), strict=True)):
                continue
            bucket = side, keys
            columns[bucket] = columns.get(bucket, 0) + 1
            ranks.setdefault(bucket, index / len(order))
    by_source = {side: {} for side in wanted}
    if not columns:
        return by_source, cell

    rows, cols, coefficients = [], [], []
    for column, (side, keys) in enumerate(columns):
        constraints = [('source', side), *zip(quotas, keys, strict=True)]
        for key in constraints:
            rows.append(row_numbers[key])
            cols.append(column)
            coefficients.append(1.)

    # Fill the available source slots first, then minimize shares over target.
    # Positive shares may flex when the requested balance is impossible. Zero
    # shares are excluded above. Weighted draw order breaks equally good fits.
    axis_rows = len(limits) - len(wanted)
    rows.extend(range(len(wanted), len(limits)))
    cols.extend(range(len(columns), len(columns) + axis_rows))
    coefficients.extend([-1.] * axis_rows)
    matrix = csc_array((coefficients, (rows, cols)), shape=(len(limits), len(columns) + axis_rows))
    slots = sum(wanted.values())
    fill_value = slots * len(quotas) + 1
    costs = [-fill_value + rank / (slots + 1) for rank in ranks.values()] + [1.] * axis_rows
    result = milp(costs, integrality=[1] * len(columns) + [0] * axis_rows,
                  bounds=Bounds(0, [*columns.values(), *([slots] * axis_rows)]),
                  constraints=LinearConstraint(matrix, ub=list(limits.values())),
                  options={'mip_rel_gap': 0.0})
    if not result.success:
        return None

    for (side, bucket), count in zip(columns, result.x[:len(columns)], strict=True):
        if round(count):
            by_source[side][bucket] = round(count)
    return by_source, cell


def _feat(item):
    return {"artist": item.get("artist") or "", "genre": item.get("genre") or "",
            'artist_ids': item.get('artist_ids'),
            "family": item.get('family') or '',
            "source": item.get("source") or "theirs"}


def _is_overlap(candidate, state):
    if (candidate.get("passenger_similarity") or 0) >= CROSSOVER_SIMILARITY:
        return True
    inputs = state.get("inputs") or {}
    if selected_artist(candidate, inputs.get("artists", []), inputs.get("artist_refs", {})):
        return True
    families = state.get('genre_families')
    wanted = {families.get(genre) if families is not None else genre_map.family(genre)
              for genre in inputs.get("genres", []) if genre}
    family = (candidate.get('family') if families is not None else genre_map.family(candidate.get('genre')))
    return bool(family and family in wanted)


def _mix_source(candidate, state):
    if candidate.get("source") != "mine":
        return "theirs"
    return "blend" if _is_overlap(candidate, state) else "yours"


def _mix_weights(state):
    blend = 1 / 3 if state.get("blend_available") else 0
    own = state.get("own_pct", 50) / 100
    return {"yours": (1 - blend) * own, "theirs": (1 - blend) * (1 - own), "blend": blend}


def _pick_mix(state, groups, orders, target_s):
    weights = _mix_weights(state)
    seeds = state.get("inputs", {})
    artists, refs = seeds.get("artists", []), seeds.get("artist_refs", {})
    orders["theirs"] = _prefer_seed_artists(orders["theirs"], artists, refs)
    averages = {side: (sum(c["duration"] or AVG_TRACK_S for c in rows) / len(rows)
                       if rows else AVG_TRACK_S) for side, rows in groups.items()}

    def fill_party(party, wanted):
        sources = {side: orders[side] for side in wanted}
        weights = _quota_weights(state, party)
        quotas = _quotas(weights, sum(wanted.values()))
        caps = {side: _artist_cap(groups[side], count) for side, count in wanted.items()}
        preferred = artists if party == 'theirs' else ()
        draws = {side: _fill(rows, wanted[side], caps[side], quotas, preferred, refs)
                 for side, rows in sources.items()}
        if not quotas or not any(wanted.values()):
            return draws

        tracks = [candidate for picks, _ in draws.values() for candidate in picks]
        if (all(len(draws[side][0]) == count for side, count in wanted.items())
                and not missed_balances(tracks, quotas)):
            return draws
        fit = _joint_quotas(sources, wanted, weights, quotas)
        if fit is None:
            return draws

        cells, cell_for = fit
        return {side: _fill(rows, wanted[side], caps[side], {'cell': cells[side]}, preferred, refs,
                            bucket_for=lambda candidate, _kind, _quota: cell_for(candidate))
                for side, rows in sources.items()}

    def fill(wanted):
        return {**fill_party('mine', {side: wanted[side] for side in ('yours', 'blend')}),
                **fill_party('theirs', {'theirs': wanted['theirs']})}

    attempts = {}

    def draw(count):
        if count in attempts:
            return attempts[count]
        wanted = _apportion_percentages(weights, count)
        allocation = fill(wanted)
        picked, seconds, short = {}, {}, {}
        expected_s = 0
        for side, slots in wanted.items():
            picked[side], seconds[side] = allocation[side]
            avg = seconds[side] / len(picked[side]) if picked[side] else averages[side]
            missing_s = (slots - len(picked[side])) * avg
            expected_s += seconds[side] + missing_s
            if missing_s:
                short["mine" if side == "yours" else side] = round(missing_s / 60)
        if not state.get("building"):
            gap = count - sum(map(len, picked.values()))
            for side in ("yours", "theirs", "blend"):
                if not gap:
                    break
                party = 'theirs' if side == 'theirs' else 'mine'
                sources = ('theirs',) if party == 'theirs' else ('yours', 'blend')
                expanded = {pool: len(picked[pool]) for pool in sources}
                expanded[side] += gap
                replacement = fill_party(party, expanded)
                added = sum(len(rows) - len(picked[pool]) for pool, (rows, _) in replacement.items())
                if added > 0:
                    for pool, (rows, secs) in replacement.items():
                        picked[pool], seconds[pool] = rows, secs
                    gap -= added
        secs = sum(seconds.values())
        attempts[count] = (picked, short, expected_s if state.get("building") else secs)
        return attempts[count]

    avg = sum(weights[side] * averages[side] for side in weights)
    count = max(1, round(target_s / max(avg, 60)))
    capacity = max(count, sum(map(len, groups.values())))
    for _ in range(8):
        picked, _, secs = draw(count)
        if not secs:
            break
        next_count = max(1, min(capacity, round(count * target_s / secs)))
        if next_count in attempts:
            break
        count = next_count
    best = min(attempts, key=lambda n: abs(attempts[n][2] - target_s))
    for count in (best - 1, best + 1):
        if 1 <= count <= capacity:
            draw(count)
    picked, short, _ = min(attempts.values(), key=lambda attempt: abs(attempt[2] - target_s))
    return picked, short


def construct(request: MixRequest, rng: RandomSource) -> MixResult:
    """Allocate a fixed candidate snapshot using the caller's explicit random source.

    Shortfalls remain explicit, including while discovery is provisional. Acquisition
    decides eligibility; construction never searches, looks up metadata or persists.
    """
    state = {"own_pct": request.own_pct, "targets": request.targets, "building": request.building,
             "inputs": {"artists": request.artists, "genres": request.genres, "artist_refs": request.artist_refs},
             "genre_families":request.genre_families}
    groups = {"yours": [], "theirs": [], "blend": []}
    banned, penalized = set(request.banned), set(request.previous)
    unique = {}
    for candidate in request.candidates:
        video = candidate["video_id"]
        if video in banned:
            continue
        # The current library owns an upload also found in the cached passenger pool.
        # Resolve ownership before group sizes, durations and quota allocation are computed.
        # This also handles songs saved after passenger acquisition, in either input order.
        previous = unique.get(video)
        if previous is None or (candidate["source"] == "mine" and previous["source"] != "mine"):
            unique[video] = candidate
    for candidate in unique.values():
        candidate = request.ranking.annotate(candidate)
        groups[_mix_source(candidate, state)].append(candidate)
    state["blend_available"] = bool(groups["blend"])
    orders = {side: sample_order(rows, rng, request.familiarity_pct / 100, penalized, request.ranking)
              for side, rows in groups.items()}
    picked, short = _pick_mix(state, groups, orders, request.target_minutes * 60)
    ordered = journey_order(picked["yours"] + picked["blend"] + picked["theirs"], "road_trip", request.seed, _feat)
    return MixResult(ordered, picked, groups, short, state["blend_available"])
