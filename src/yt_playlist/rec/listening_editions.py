"""Held daily editions, selected using actual visible editorial exposure."""
import hashlib

from yt_playlist.rec.listening_dates import edition_day
from yt_playlist.rec.listening_insights import candidates


def prepare(data, repo, now):
    key = edition_day(now, data['timezone'])
    previous = repo.prepared('listening_edition') or {}
    pool = candidates(data)
    valid = {item['id']: item for item in pool}
    prior = previous.get('stories', [])
    # Evidence refreshes must not move the page beneath an active reader.
    if previous.get('day') == key and prior and all(s['id'] in valid for s in prior):
        return previous
    views = repo.impressions()
    for item in pool:
        novelty = 0 if item['id'] in views else 1
        item['score'] = .35*item['strength'] + .25*item['departure'] + .25*novelty + .15*item['timeliness']
        item['tie'] = hashlib.sha256((key + item['id']).encode()).hexdigest()
    pool.sort(key=lambda item: (-item['score'], item['tie']))
    recent_subjects = set().union(*(set(s) for s in previous.get('recent_subjects', [])[:2]))
    fresh = [s for s in pool if (s['id'] not in views or now-views[s['id']] >= 7*86400)
             and s['subject'] not in recent_subjects]
    # A valid held edition is preferable to a fake new headline. When another
    # supported lead exists it wins, even if its raw count is lower.
    if not fresh and prior and all(s['id'] in valid for s in prior):
        return {**previous, 'day': key}
    ordered = fresh + [s for s in pool if s not in fresh]
    selected, families, subjects = [], set(), set()
    for item in ordered:
        if item['family'] in families or item['subject'] in subjects:
            continue
        selected.append({k: v for k, v in item.items() if k not in ('score', 'tie', 'strength', 'departure', 'timeliness')})
        families.add(item['family'])
        subjects.add(item['subject'])
        if len(selected) == 4:
            break
    identity = hashlib.sha256((key + '|'.join(s['id'] for s in selected)).encode()).hexdigest()[:24]
    return {'id': identity, 'day': key, 'built_at': now, 'timezone': data['timezone'],
            'stories': selected, 'available': bool(selected),
            'recent_subjects': [sorted(subjects), *previous.get('recent_subjects', [])[:1]]}
