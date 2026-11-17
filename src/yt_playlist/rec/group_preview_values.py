"""Detached eligibility and diversity rules for group candidate frames."""
from yt_playlist.rec import mode_rules


def candidate_frame(pool, members, blocked, muted, cap_artist, cap_album):
    eligible, seen = [], set()
    for item in pool:
        key = item.get('key')
        if key in members and key not in blocked and key not in seen and item.get('artist') not in muted:
            eligible.append(item)
            seen.add(key)
    diverse = mode_rules._diversify(eligible, cap_artist, cap_album)
    return {'eligible': len(diverse), 'full_mix': len(diverse) >= mode_rules.PROTO_SIZE,
        'tracks': [{field: item.get(field) for field in ('key', 'title', 'artist')}
                   for item in diverse[:mode_rules.PROTO_SIZE]],
        'keys': [item['key'] for item in eligible]}
