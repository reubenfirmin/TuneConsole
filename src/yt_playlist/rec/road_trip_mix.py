"""Pure road-trip allocation and ordering; no repositories, providers or draft writes."""
import math
from collections import Counter, deque
from dataclasses import dataclass
from itertools import zip_longest
from typing import Protocol

from yt_playlist.rec.journeys import journey_order
from yt_playlist.util import genre_map

AVG_TRACK_S = 210
FAMILIARITY_SIGMA = 0.3
REPEAT_PENALTY = 0.3


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


@dataclass(frozen=True)
class MixResult:
    ordered: list[dict]
    picked: dict[str, list[dict]]
    groups: dict[str, list[dict]]
    short: dict[str, int]
    blend_available: bool


def _weight(cand, familiarity, penalized):
    w = cand["score"]
    w *= math.exp(-((cand["fam"] - familiarity) ** 2) / (2 * FAMILIARITY_SIGMA ** 2)) + 0.05
    if cand["video_id"] in penalized:
        w *= REPEAT_PENALTY
    return max(w, 0.0)


def _sample_order(cands, rng, weight_of):
    keyed = []
    for candidate in cands:
        weight = weight_of(candidate)
        if weight > 0:
            keyed.append((rng.random() ** (1.0 / weight), candidate))
    keyed.sort(key=lambda pair: -pair[0])
    return [candidate for _, candidate in keyed]


def _artist_cap(cands, needed):
    artists = {c["artist"] for c in cands if c["artist"]}
    return max(2, math.ceil(needed / max(1, len(artists))) + 1)


def _axis_genre(cand):
    return cand.get("cluster_genre") or cand["genre"] or cand["family"] or ""


def _bucket(cand, kind, quota):
    key = ("genre:" + _axis_genre(cand)) if kind == "genre" else ("era:" + cand["decade"])
    return key if key in quota else ""


def _prefer_seed_artists(order, artists):
    groups = {}
    for candidate in order:
        groups.setdefault(_axis_genre(candidate), []).append(candidate)
    queues = {}
    for genre, rows in groups.items():
        named, related = {}, []
        for candidate in rows:
            artist = candidate["artist"].casefold()
            if artist in artists:
                named.setdefault(artist, []).append(candidate)
            else:
                related.append(candidate)
        seeds = [c for group in zip_longest(*named.values()) for c in group if c]
        queues[genre] = deque(c for group in zip_longest(seeds[::2], seeds[1::2], related) for c in group if c)
    return [queues[_axis_genre(c)].popleft() for c in order]


def _quota_weights(state, party, cands, conditional=False):
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
    if conditional:
        eligible = [c for c in cands if all(q[_bucket(c, kind, q)] > 0 for kind, q in out.items())]
        for kind, weights in out.items():
            available = {_bucket(c, kind, weights) for c in eligible}
            weights.update({k: 0 for k in weights if k not in available})
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


def _quotas(state, party, slots, cands, conditional=False):
    return {kind: (_apportion_percentages(weights, slots) if any(weights.values())
                   else dict.fromkeys(weights, 0))
            for kind, weights in _quota_weights(state, party, cands, conditional).items()}


def _fill(order, slots, cap, quotas=None, preferred_artists=()):
    quotas = quotas or {}
    picked, per, taken = [], Counter(), set()
    spent = {kind: Counter() for kind in quotas}
    total = 0.0

    def blocked(candidate):
        if len(picked) >= slots:
            return True
        if (candidate["artist"] and candidate["artist"].casefold() not in preferred_artists
                and per[candidate["artist"]] >= cap):
            return True
        return any(spent[kind][_bucket(candidate, kind, quota)] + 1 > quota.get(_bucket(candidate, kind, quota), 0)
                   for kind, quota in quotas.items())

    def take(candidate):
        nonlocal total
        for kind, quota in quotas.items():
            spent[kind][_bucket(candidate, kind, quota)] += 1
        per[candidate["artist"]] += 1
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
                if candidate["video_id"] in taken or _bucket(candidate, kind, quota) != key or blocked(candidate):
                    continue
                take(candidate)
    for candidate in order:
        if candidate["video_id"] not in taken and not blocked(candidate):
            take(candidate)
    return picked, total


def _feat(item):
    return {"artist": item.get("artist") or "", "genre": item.get("genre") or "",
            "source": item.get("source") or "theirs"}


def _is_overlap(candidate, state):
    inputs = state.get("inputs") or {}
    artists = {artist.strip().lower() for artist in inputs.get("artists", []) if artist}
    if (candidate.get("artist") or "").strip().lower() in artists:
        return True
    wanted = {genre_map.family(genre) for genre in inputs.get("genres", []) if genre}
    return bool(wanted and genre_map.family(candidate.get("genre")) in wanted)


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
    artists = {artist.casefold() for artist in state.get("inputs", {}).get("artists", [])}
    orders["theirs"] = _prefer_seed_artists(orders["theirs"], artists)
    averages = {side: (sum(c["duration"] or AVG_TRACK_S for c in rows) / len(rows)
                       if rows else AVG_TRACK_S) for side, rows in groups.items()}

    def fill(side, count):
        party = "theirs" if side == "theirs" else "mine"
        quotas = _quotas(state, party, count, groups[side], conditional=party == "mine" and state["blend_available"])
        return _fill(orders[side], count, _artist_cap(groups[side], count), quotas, artists if side == "theirs" else ())

    attempts = {}

    def draw(count):
        if count in attempts:
            return attempts[count]
        wanted = _apportion_percentages(weights, count)
        picked, seconds, short = {}, {}, {}
        expected_s = 0
        for side, slots in wanted.items():
            picked[side], seconds[side] = fill(side, slots)
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
                replacement, secs = fill(side, len(picked[side]) + gap)
                added = len(replacement) - len(picked[side])
                if added > 0:
                    picked[side], seconds[side] = replacement, secs
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
             "inputs": {"artists": request.artists, "genres": request.genres}}
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
        groups[_mix_source(candidate, state)].append(candidate)
    state["blend_available"] = bool(groups["blend"])
    orders = {side: _sample_order(rows, rng, lambda c: _weight(c, request.familiarity_pct / 100, penalized))
              for side, rows in groups.items()}
    picked, short = _pick_mix(state, groups, orders, request.target_minutes * 60)
    ordered = journey_order(picked["yours"] + picked["blend"] + picked["theirs"], "road_trip", request.seed, _feat)
    return MixResult(ordered, picked, groups, short, state["blend_available"])
