"""Factual, deterministic editorial detectors. No model or preference writes."""
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
from urllib.parse import urlencode


def shifted(day, days):
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def candidates(data):
    today, catalog = data['today'], data['catalog']
    columns = data['columns']
    rows = [dict(zip(columns, row)) for row in data['rows']]
    tracks, artists = defaultdict(list), defaultdict(list)
    for row in rows:
        tracks[row['track']].append(row)
        if row['artist_id']:
            artists[row['artist_id']].append(row)
    earliest = min((r['day'] for r in rows if r['plays'] or r['sightings']), default=today)
    findings = []

    def add(family, subject, title, description, evidence, *, since=None, until=None,
            keys=(), visual='score', strength=.6, departure=.5, anchor=None, filters=None):
        since, until = since or shifted(today, -13), until or shifted(today, 1)
        selected = [r for r in rows if since <= r['day'] < until and (not keys or r['track'] in keys)]
        relevant = [catalog[k] for k in keys if k in catalog]
        art = next((m['thumbnail'] for m in relevant if m.get('thumbnail')), None)
        signature = hashlib.sha256(f'{family}|{subject}|{anchor or since}'.encode()).hexdigest()[:24]
        timeline = []
        for key in keys[:7]:
            meta = catalog[key]
            counts = Counter()
            for r in selected:
                if r['track'] == key:
                    counts[r['day']] += r['plays'] or r['sightings']
            timeline.append({'id': key, 'name': meta.get('title') or 'Untitled',
                             'artist': meta.get('artist') or 'Unknown artist',
                             'thumbnail': meta.get('thumbnail'), 'counts': dict(sorted(counts.items()))})
        query = {'view': 'analytics', 'window': 'custom', 'start': since, 'end': until, **(filters or {})}
        if len(keys) == 1:
            query['track'] = keys[0]
        findings.append({'id': signature, 'family': family, 'subject': subject, 'title': title,
            'description': description, 'evidence': evidence, 'visual': visual, 'art': art,
            'since': since, 'until': until, 'timeline': timeline, 'strength': min(1, strength),
            'departure': min(1, departure), 'timeliness': 1 if until > shifted(today, -2) else .65,
            'url': '/charts?' + urlencode(query), 'timezone': data['timezone']})

    # One artist can anchor a small library. Listening days, never repeated
    # telemetry samples, establish independent occasions.
    for artist_id, history in artists.items():
        recent = [r for r in history if r['day'] >= shifted(today, -13) and r['plays']]
        if not recent:
            continue
        keys = sorted({r['track'] for r in recent}, key=lambda k: -sum(r['plays'] for r in recent if r['track'] == k))
        name = catalog[keys[0]]['artist']
        days = sorted({r['day'] for r in recent})
        plays = sum(r['plays'] for r in recent)
        byday = Counter()
        for r in history:
            if r['plays']:
                byday[r['day']] += r['plays']
        prior_days = [d for d in byday if d < shifted(today, -13)]
        first = min(byday)
        common = {'keys': keys, 'filters': {'artist': artist_id}}
        if len(days) >= 3 and plays >= 6:
            if first >= shifted(today, -27) and first >= shifted(earliest, 7):
                add('attachment', 'artist:' + artist_id, f'{name}, on repeat.',
                    'An introduction is becoming a recurring presence.',
                    f'{plays} plays across {len(days)} listening days in the last 14 days.',
                    visual='returns', strength=len(days)/7, anchor=first, **common)
            elif prior_days and min(prior_days) <= shifted(today, -30):
                add('familiar', 'artist:' + artist_id, f'Back in the company of {name}.',
                    'A familiar part of your listening is holding its place.',
                    f'{plays} plays on {len(days)} days recently; earlier listening goes back to {first}.',
                    strength=len(days)/7, **common)
        current = sum(n for d, n in byday.items() if d >= shifted(today, -6))
        previous = sum(n for d, n in byday.items() if shifted(today, -13) <= d < shifted(today, -6))
        active = sum(d >= shifted(today, -6) for d in byday)
        # Comparisons need a complete earlier history, not a truncated import.
        if earliest <= shifted(today, -20) and current >= 10 and active >= 3 and previous and (
                current >= previous*2 and current-previous >= 5):
            add('rising', 'artist:' + artist_id, f'{name} is taking up more room.',
                'The past seven calendar days have a different center of gravity.',
                f'{current} plays in the current seven days, against {previous} in the preceding seven. Today is partial.',
                since=shifted(today, -13), strength=current/30, departure=(current/previous-1)/3, **common)
        if len(prior_days) >= 2 and len(days) >= 2 and (
                date.fromisoformat(days[0])-date.fromisoformat(max(prior_days))).days >= 60:
            gap = (date.fromisoformat(days[0])-date.fromisoformat(max(prior_days))).days
            add('return', 'artist:' + artist_id, f'{name}, after the quiet.',
                'An old connection has started sounding again.',
                f'{gap} days between recorded visits. Now {plays} plays on {len(days)} separate days.',
                visual='returns', anchor=days[0], strength=len(days)/5, departure=1, **common)
        familiar_tracks = [k for k in keys if data['first_seen'].get(k, today) < shifted(today, -30)]
        new_tracks = [k for k in keys if data['first_seen'].get(k, earliest) >= max(shifted(today, -13), shifted(earliest, 7))]
        new_days = {r['day'] for r in recent if r['track'] in new_tracks}
        if familiar_tracks and len(new_tracks) >= 2 and len(new_days) >= 2:
            add('deeper', 'artist:' + artist_id, f'There’s more to {name}.',
                'You are finding new recordings by an artist you already know.',
                f'{len(new_tracks)} first recorded songs across {len(new_days)} days, alongside earlier favorites.',
                **common)

    for key, history in tracks.items():
        meta = catalog.get(key, {})
        actions = [r for r in history if r['day'] >= shifted(today, -13) and (r['hearts'] or r['skulls'])]
        if not actions:
            continue
        last = max(actions, key=lambda r: r['day'])
        before = {r['day'] for r in history if r['plays'] and r['day'] < last['day']}
        heart = bool(last['hearts'])
        slow = heart and len(before) >= 3
        title = meta.get('title') or 'This song'
        add('slow_heart' if slow else 'decision', 'track:' + key,
            f'{title}. {"A slow yes." if slow else "A yes." if heart else "A line drawn."}',
            'A considered favorite.' if slow else 'A confirmed choice in your recent listening.',
            f'{len(before)} prior listening days before the heart on {last["day"]}.' if slow else
            f'A {"heart" if heart else "skull"} recorded on {last["day"]}. Clears and reversals remain in the history.',
            keys=[key], visual='decisions', anchor=last['day'])

    recent = [r for r in rows if r['day'] >= shifted(today, -13)]
    seconds = Counter()
    for r in recent:
        seconds[r['format']] += r['seconds']
    measured_days = {r['day'] for r in recent if r['seconds']}
    labels = {'mixes': 'Mix tapes', 'albums': 'Long play', 'radio': 'On air', 'liked': 'Liked Music', 'other': 'Other / unknown'}
    if sum(seconds.values()) >= 1800 and len(measured_days) >= 3:
        dominant = max(seconds, key=seconds.get)
        share = round(seconds[dominant]/sum(seconds.values())*100)
        keys = sorted({r['track'] for r in recent if r['format'] == dominant and r['seconds']},
                      key=lambda k: -sum(r['seconds'] for r in recent if r['track'] == k))[:5]
        add('formats', 'format:' + dominant, f'{labels[dominant]}. Your recent rhythm.',
            'Different ways of listening leave different shapes.',
            f'{round(sum(seconds.values())/60)} measured minutes across {len(measured_days)} days; {share}% in {labels[dominant]}.',
            visual='formats', keys=keys, filters={'format': dominant}, strength=len(measured_days)/7)
        findings[-1]['bands'] = [{'key': k, 'name': labels[k], 'seconds': seconds[k]} for k in labels if seconds[k]]

    # Only mature cohorts have had time to return. The beginning of an import
    # is left censored; it cannot establish newness.
    cohort = [k for k, first in data['first_seen'].items()
              if shifted(earliest, 7) <= first <= shifted(today, -14) and first >= shifted(today, -28)]
    regulars = []
    for key in cohort:
        first = data['first_seen'][key]
        returns = {r['day'] for r in tracks[key] if first < r['day'] <= shifted(first, 14) and r['plays']}
        if len(returns) >= 2:
            regulars.append(key)
    if len(cohort) >= 5 and regulars:
        add('adoption', 'cohort:' + shifted(today, -28), 'Some introductions stayed.',
            'The interesting part of discovery is what happens after the first play.',
            f'{len(regulars)} of {len(cohort)} mature discoveries returned on at least two later days within 14 days.',
            keys=regulars[:7], visual='returns', since=shifted(today, -28), strength=len(cohort)/15)
    return findings
