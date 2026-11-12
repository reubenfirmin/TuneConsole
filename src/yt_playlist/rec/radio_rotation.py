"""Radio's listening rotation, independent of prepared recommendation lanes.

Midpoint gives heard, unplayed-library and unheard-unowned recordings equal
opportunities within the current musical/eligibility constraints. The endpoints
interpolate to familiar music at Comfort and 85% unheard-unowned / 15% unplayed
library at Discovery. A missing category lends its share to the available ones.
"""
from collections import Counter
import math

KINDS = ('familiar', 'unplayed', 'new')
WINDOW = 24


def shares(comfort):
    if comfort <= .5:
        mix = comfort * 2
        return dict(zip(KINDS, (mix / 3, .15 * (1 - mix) + mix / 3,
                               .85 * (1 - mix) + mix / 3)))
    mix = (comfort - .5) * 2
    return dict(zip(KINDS, ((1 - mix) / 3 + mix, (1 - mix) / 3, (1 - mix) / 3)))


def classify(fact):
    if fact is None:
        # Missing identity/evidence cannot establish novelty.
        return 'familiar'
    return 'familiar' if fact['heard'] else 'unplayed' if fact['owned'] else 'new'


def metadata(fact):
    kind = classify(fact)
    return dict(_radio_rotation=kind, _radio_fresh=kind == 'new',
                _radio_last_started=fact.get('last_started') if fact else None,
                _radio_last_offered=fact.get('last_offered') if fact else None)


def issue(track):
    """Stamp a newly selected, confirmed identity using its current facts."""
    kind = track.get('_radio_rotation')
    if kind in KINDS:
        track['_radio_rotation_slot'] = kind
    else:
        track.pop('_radio_rotation_slot', None)


def next_kind(history, comfort, available=KINDS):
    weights = shares(comfort)
    total = sum(weights[kind] for kind in available)
    if not total:
        return None
    recent = [track['_radio_rotation_slot'] for track in history[-WINDOW:]
              if track.get('_radio_rotation_slot') in KINDS]
    # Remember the allocation at issue time. Listening changes current novelty,
    # but must not retrospectively turn every completed discovery slot into a
    # familiar slot and thereby starve familiar music from the next sequence.
    # Older checkpoints have no issue-time evidence. Ignore unknown allocations
    # rather than treating songs started in this session as prior favourites.
    counts = Counter(recent)
    return max((kind for kind in available if weights[kind]), key=lambda kind: (
        weights[kind] / total * (len(recent) + 1) - counts[kind], weights[kind], kind))


def source(eligible, history, comfort):
    wanted = next_kind(history, comfort, {track['_radio_rotation'] for track in eligible})
    if wanted is None:
        return eligible
    return [track for track in eligible if track['_radio_rotation'] == wanted]


def reserve(tracks, limit, comfort):
    """Keep a bounded reserve for each desired category, in input rank order."""
    groups = {kind: [track for track in tracks if track['_radio_rotation'] == kind] for kind in KINDS}
    weights, counts, result = shares(comfort), Counter(), []
    while groups and len(result) < limit:
        groups = {kind: rows for kind, rows in groups.items() if rows}
        if not groups:
            break
        total = sum(weights[kind] for kind in groups)
        kind = max(groups, key=lambda kind: (
            weights[kind] / total * (len(result) + 1) - counts[kind] if total else -counts[kind],
            weights[kind], kind))
        result.append(groups[kind].pop(0))
        counts[kind] += 1
    return result


def repetition(track, now):
    """Soft rest after starts/visible offers; favourites always remain eligible."""
    played, shown = track.get('_radio_last_started'), track.get('_radio_last_offered')
    return .6 * max(math.exp(-max(0, now - played) / (7 * 86400)) if played is not None else 0,
                    math.exp(-max(0, now - shown) / 86400) if shown is not None else 0)
