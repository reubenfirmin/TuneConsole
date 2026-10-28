"""Factual, deterministic editorial detectors. No model or preference writes."""
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
from urllib.parse import urlencode


def shifted(day, days):
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def readable(day):
    value = date.fromisoformat(day)
    return f'{value:%B} {value.day}'


def candidates(data):
    today, catalog = data['today'], data['catalog']
    week = shifted(today, -date.fromisoformat(today).weekday())
    columns = data['columns']
    rows = [dict(zip(columns, row)) for row in data['rows']]
    tracks, artists = defaultdict(list), defaultdict(list)
    for row in rows:
        tracks[row['track']].append(row)
        if row['artist_id']:
            artists[row['artist_id']].append(row)
    earliest = min((r['day'] for r in rows if r['plays'] or r['sightings']), default=today)
    findings = []

    def add(family, subject, title, evidence, *, since=None, until=None,
            keys=(), visual='score', strength=.6, departure=.5, anchor=None, filters=None,
            stat=None, stat_label=None, decision=None):
        since, until = since or shifted(today, -13), until or shifted(today, 1)
        selected = [r for r in rows if since <= r['day'] < until and (not keys or r['track'] in keys)]
        relevant = [catalog[k] for k in keys if k in catalog]
        art = next((m['thumbnail'] for m in relevant if m.get('thumbnail')), None)
        signature = hashlib.sha256(f'{family}|{subject}|{anchor or week}'.encode()).hexdigest()[:24]

        def song(key):
            meta = catalog[key]
            return {'id': key, 'name': meta.get('title') or 'Untitled',
                    'artist': meta.get('artist') or 'Unknown artist',
                    'thumbnail': meta.get('thumbnail'), 'video_id': meta.get('video_id')}

        timeline = []
        for key in keys[:7]:
            counts = Counter()
            for r in selected:
                if r['track'] == key:
                    counts[r['day']] += r['plays']
            timeline.append({**song(key),
                             'counts': dict(sorted((d, n) for d, n in counts.items() if n))})
        if family in ('return', 'attachment') and relevant:
            counts = Counter()
            daily_songs = defaultdict(Counter)
            for row in selected:
                counts[row['day']] += row['plays']
                if row['plays']:
                    daily_songs[row['day']][row['track']] += row['plays']
            # One actual recording per listening day. Stable ties avoid swapping
            # artwork when source rows arrive in a different order.
            songs = {}
            for day, plays in sorted(daily_songs.items()):
                key = min(plays, key=lambda k: (-plays[k], not catalog[k].get('video_id'), k))
                songs[day] = song(key)
            timeline = [{'id': subject, 'name': relevant[0].get('artist') or 'Unknown artist',
                         'artist': relevant[0].get('artist') or 'Unknown artist',
                         'thumbnail': art, 'songs': songs,
                         'counts': dict(sorted((d, n) for d, n in counts.items() if n))}]
        query = {'view': 'analytics', 'window': 'custom', 'start': since, 'end': until, **(filters or {})}
        if subject.startswith('track:') and len(keys) == 1:
            query['track'] = keys[0]
        measures = (('seconds',) if visual == 'formats' else
                    ('hearts', 'skulls', 'clears') if visual in ('decisions', 'rating_actions') else ('plays',))
        activity = rows if visual == 'formats' else selected
        latest = max((r['day'] for r in activity if since <= r['day'] < until
                      and any(r[measure] for measure in measures)), default=shifted(until, -1))
        age = max(1, (date.fromisoformat(today)-date.fromisoformat(latest)).days)
        findings.append({'id': signature, 'family': family, 'subject': subject, 'title': title,
            'evidence': evidence, 'visual': visual, 'art': art,
            'stat': stat, 'stat_label': stat_label, 'decision': decision,
            'since': since, 'until': until, 'timeline': timeline, 'strength': min(1, strength),
            'departure': min(1, departure), 'timeliness': 1/age,
            'url': '/charts?' + urlencode(query, doseq=True), 'timezone': data['timezone'],
            'period_label': f'{date.fromisoformat(since):%b %d} – {date.fromisoformat(shifted(until, -1)):%b %d, %Y}'})

    # One artist can anchor a small library. Listening days, never repeated
    # telemetry samples, establish independent occasions.
    for artist_id, history in artists.items():
        recent = [r for r in history if r['day'] >= shifted(today, -13) and r['plays']]
        if not recent:
            continue
        keys = sorted({r['track'] for r in recent}, key=lambda k: (-sum(r['plays'] for r in recent if r['track'] == k), k))
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
                    f'You started listening to {name} on {readable(first)}. You’ve played them {plays} times over {len(days)} days in the past two weeks.',
                    visual='returns', strength=len(days)/7, anchor=first, **common)
            elif prior_days and min(prior_days) <= shifted(today, -30):
                add('familiar', 'artist:' + artist_id, f'Still coming back to {name}.',
                    f'{plays} plays over {len(days)} days in the past two weeks. You’ve been listening to {name} since at least {date.fromisoformat(first):%B %Y}.',
                    strength=len(days)/7, **common)
        current = sum(n for d, n in byday.items() if shifted(today, -7) <= d < today)
        previous = sum(n for d, n in byday.items() if shifted(today, -14) <= d < shifted(today, -7))
        active = sum(shifted(today, -7) <= d < today for d in byday)
        # Comparisons need a complete earlier history, not a truncated import.
        if earliest <= shifted(today, -20) and current >= 10 and active >= 3 and previous and (
                current >= previous*2 and current-previous >= 5):
            add('rising', 'artist:' + artist_id, f'More {name} lately.',
                f'{current} plays in the past week, against {previous} the week before.',
                since=shifted(today, -14), until=today, strength=min(1,current/20)*min(1,active/5),
                departure=(current/previous-1)/3, **common)
        if len(prior_days) >= 2 and len(days) >= 3 and plays >= 6 and (
                date.fromisoformat(days[0])-date.fromisoformat(max(prior_days))).days >= 60:
            gap = (date.fromisoformat(days[0])-date.fromisoformat(max(prior_days))).days
            add('return', 'artist:' + artist_id, f'Back to {name}.',
                f'You picked up {name} again after {gap} days away. You’ve played them {plays} times over {len(days)} days in the past two weeks.',
                visual='returns', stat=gap, stat_label='days between plays',
                anchor=days[0], strength=min(len(days)/5, plays/12), departure=1, **common)
        familiar_tracks = [k for k in keys if data['first_seen'].get(k, today) < shifted(today, -30)]
        new_tracks = [k for k in keys if data['first_seen'].get(k, earliest) >= max(shifted(today, -13), shifted(earliest, 7))]
        new_days = {r['day'] for r in recent if r['track'] in new_tracks}
        if familiar_tracks and len(new_tracks) >= 2 and len(new_days) >= 2:
            add('deeper', 'artist:' + artist_id, f'There’s more to {name}.',
                f'Alongside the songs you know, you’ve tried {len(new_tracks)} more by {name} over {len(new_days)} days.',
                strength=min(len(new_tracks)/5, len(new_days)/4),
                departure=len(new_tracks)/len(keys),
                keys=new_tracks, filters={'artist': artist_id, 'track': new_tracks})

    # A heart after repeated earlier listening can reveal a change in taste.
    # Ordinary heart/skull announcements never become editorial panels.
    for key, history in tracks.items():
        actions = [r for r in history if r['day'] >= shifted(today, -13) and r['hearts']]
        if not actions:
            continue
        last = max(actions, key=lambda r: r['day'])
        before = {r['day'] for r in history if r['plays'] and r['day'] < last['day']}
        if len(before) < 3:
            continue
        title = catalog.get(key, {}).get('title') or 'This song'
        add('slow_heart', 'track:' + key, f'{title}. A slow-earned heart.',
            f'You’d listened on {len(before)} days before you gave this one a heart.',
            keys=[key], visual='decisions', decision='heart', anchor=last['day'])

    recent = [r for r in rows if r['day'] >= shifted(today, -13)]
    plays = sum(r['plays'] for r in recent)
    rated = sum(r['rated'] for r in recent)
    liked = sum(r['liked'] for r in recent)
    rated_days = {r['day'] for r in recent if r['rated']}
    if rated >= 20 and plays and rated/plays >= .7 and len(rated_days) >= 3:
        share = round(liked/rated*100)
        add('liked_starts', 'ratings:starts', 'How often you return to hearted songs.',
            f'{share}% of plays with a known starting rating were already hearted ({liked:,} of {rated:,}). '
            f'The starting rating was captured for {rated:,} of {plays:,} plays; {plays-rated:,} remain unknown.',
            visual='rating_share', strength=.75 if rated >= 10 else .4,
            stat=share, stat_label='of known starting ratings')
        findings[-1]['rating_counts'] = {'liked': liked, 'not_liked': rated-liked, 'unknown': plays-rated}
    actions = {kind: sum(r[kind] for r in recent) for kind in ('hearts', 'skulls', 'clears')}
    action_days = {r['day'] for r in recent if any(r[k] for k in actions)}
    action_tracks = {r['track'] for r in recent if any(r[k] for k in actions)}
    if sum(actions.values()) >= 10 and len(action_days) >= 3 and len(action_tracks) >= 5:
        add('rating_actions', 'ratings:changes', 'The choices you made.',
            f'{actions["hearts"]:,} hearts, {actions["skulls"]:,} skulls and {actions["clears"]:,} clears '
            'in the past two weeks. These are confirmed rating changes; a song can appear again after a reversal.',
            visual='rating_actions', strength=.7)
        findings[-1]['rating_days'] = [{'day': d, **{kind: sum(r[kind] for r in recent if r['day'] == d)
            for kind in actions}} for d in sorted({r['day'] for r in recent if any(r[k] for k in actions)})]
        findings[-1]['rating_counts'] = actions
    retained = data.get('retained') or {}
    hearts, skulls = retained.get('hearts', 0), retained.get('skulls', 0)
    if hearts >= 6 and retained.get('hearted_genres', 0) >= 2 and retained.get('known_genre_hearts', 0)/hearts >= .7:
        genres = retained.get('genres', [])
        known = retained.get('known_genre_hearts', 0)
        breadth = retained.get('hearted_genres', 0)
        title = f'Your hearts span {breadth} genres.'
        evidence = (f'{hearts:,} hearts and '
                    f'{skulls:,} {"skull" if skulls == 1 else "skulls"} in your current ratings. ')
        largest = retained['leading_hearted_genre']
        evidence += (f'{largest["name"]} holds {round(largest["hearts"]/known*100)}% of your genre-tagged hearts. '
                     f'{known:,} of {hearts:,} hearts have a genre. ')
        contrast = max((g for g in genres if g.get('name') and not g.get('combined')
                        and g['hearts']+g['skulls'] >= 8 and g['skulls'] > g['hearts']),
                       key=lambda g: (g['skulls']/(g['hearts']+g['skulls']), g['skulls']), default=None)
        if contrast:
            evidence += (f'{contrast["name"]} gets a different verdict: '
                         f'{contrast["hearts"]} hearts and {contrast["skulls"]} skulls. ')
        anchor = hashlib.sha256(repr((hearts, skulls, genres)).encode()).hexdigest()[:16]
        add('rating_collection', 'ratings:collection', title, evidence, visual='rating_genres',
            anchor=anchor, strength=.75)
        findings[-1]['rating_genres'] = genres
        findings[-1]['rating_counts'] = {'hearts': hearts, 'skulls': skulls}
        findings[-1]['period_label'] = 'Current ratings'

    # First encounters use the complete prepared history, including imports.
    # A new capture/import boundary cannot establish discovery rates.
    active_days = {r['day'] for r in recent if r['plays']}
    new_tracks = sum(r['new_tracks'] for r in recent)
    new_artists = sum(r['new_artists'] for r in recent)
    artist_plays = sum(r['artist_plays'] for r in recent)
    window_start = shifted(today, -13)
    baseline = [r for r in rows if r['day'] < window_start and (r['plays'] or r['sightings'])]
    baseline_days = {r['day'] for r in baseline}
    baseline_tracks = {r['track'] for r in baseline}
    returning = [r for r in recent if r['plays'] and data['first_seen'].get(r['track'], today) < window_start]
    established = (earliest <= shifted(window_start, -28) and len(baseline_days) >= 14
                   and len(baseline_tracks) >= 30 and sum(r['plays']+r['sightings'] for r in baseline) >= 100
                   and sum(r['plays'] for r in returning) >= 20 and len({r['day'] for r in returning}) >= 3)
    if (established and plays >= 60 and len(active_days) >= 5
            and (new_tracks >= 10 or new_artists >= 5)):
        add('discovery', 'discovery:recent', f'{new_tracks:,} additions to your listening history.',
            f'{new_tracks:,} of {plays:,} plays ({round(new_tracks/plays*100)}%) introduced a track '
            f'to your recorded history; {new_artists:,} introduced an artist among {artist_plays:,} '
            f'plays with a known artist. Compared with {len(baseline_days):,} earlier listening days. '
            'Listening outside this record is unknown.',
            visual='discovery', strength=min(1, len(active_days)/10), departure=new_tracks/plays,
            filters={'overlay': 'artist'})
        findings[-1]['discovery_days'] = [{'day': d, **{field: sum(r[field] for r in recent if r['day'] == d)
            for field in ('plays', 'new_tracks', 'new_artists', 'artist_plays')}} for d in sorted(active_days)]

    seconds = Counter()
    for r in recent:
        seconds[r['format']] += r['seconds']
    measured_days = {r['day'] for r in recent if r['seconds']}
    labels = {'mixes': 'Mix tapes', 'albums': 'Long play', 'radio': 'On air', 'liked': 'Liked Music', 'other': 'Other'}
    if sum(seconds.values()) >= 1800 and len(measured_days) >= 3:
        dominant = max(seconds, key=seconds.get)
        share = round(seconds[dominant]/sum(seconds.values())*100)
        keys = sorted({r['track'] for r in recent if r['format'] == dominant and r['seconds']},
                      key=lambda k: -sum(r['seconds'] for r in recent if r['track'] == k))[:5]
        balanced = len([n for n in seconds.values() if n/sum(seconds.values()) >= .15]) >= 3
        add('formats', 'format:' + dominant,
            'A little of everything.' if balanced or share<50 else f'Mostly {labels[dominant]}.',
            f'{share}% of your listening time went to {labels[dominant]}. That’s out of {round(sum(seconds.values())/60):,} minutes over {len(measured_days)} days.',
            visual='formats', keys=keys, strength=.65 if balanced else .85)
        findings[-1]['bands'] = [{'key': k, 'name': labels[k], 'seconds': seconds[k]} for k in labels if seconds[k]]
        findings[-1]['band_days'] = [{'day': d, **{fmt: sum(r['seconds'] for r in recent if r['day'] == d and r['format'] == fmt)
                                    for fmt in labels}} for d in sorted(measured_days)]

    # A completed-week comparison is eligible only when both full source periods
    # are represented. Unknown genre stays in the coverage denominator.
    older_start, newer_start = shifted(week, -14), shifted(week, -7)
    older = [r for r in rows if older_start <= r['day'] < newer_start]
    newer = [r for r in rows if newer_start <= r['day'] < week]
    if earliest <= older_start:
        old_time, new_time = Counter(), Counter()
        for r in older:
            old_time[r['format']] += r['seconds']
        for r in newer:
            new_time[r['format']] += r['seconds']
        old_total, new_total = sum(old_time.values()), sum(new_time.values())
        if min(old_total, new_total) >= 3600:
            changes = {fmt: new_time[fmt]/new_total-old_time[fmt]/old_total for fmt in labels}
            fmt = max(changes, key=changes.get)
            if changes[fmt] >= .2:
                keys = sorted({r['track'] for r in newer if r['format'] == fmt and r['seconds']})[:5]
                add('format_shift', 'format:' + fmt, f'More time in {labels[fmt]}.',
                    f'{labels[fmt]} went from {round(old_time[fmt]/old_total*100)}% to {round(new_time[fmt]/new_total*100)}% of your listening time from one week to the next.',
                    since=older_start, until=week, visual='formats', keys=keys,
                    anchor=week, departure=changes[fmt])
                findings[-1]['bands'] = [{'key': k, 'name': labels[k], 'seconds': old_time[k]+new_time[k]}
                                        for k in labels if old_time[k]+new_time[k]]
                findings[-1]['band_days'] = [{'day': d, **{k: sum(r['seconds'] for r in older+newer if r['day'] == d and r['format'] == k)
                                            for k in labels}} for d in sorted({r['day'] for r in older+newer if r['seconds']})]
        old_count, new_count = sum(r['plays'] for r in older), sum(r['plays'] for r in newer)
        old_days, new_days = {r['day'] for r in older if r['plays']}, {r['day'] for r in newer if r['plays']}
        if min(old_count, new_count) >= 20 and min(len(old_days), len(new_days)) >= 3:
            old_genre, new_genre = Counter(), Counter()
            for r in older:
                old_genre[r['genre']] += r['plays']
            for r in newer:
                new_genre[r['genre']] += r['plays']
            if min(1-old_genre['']/old_count, 1-new_genre['']/new_count) >= .7:
                changes = {g: new_genre[g]/new_count-old_genre[g]/old_count for g in new_genre if g}
                if changes:
                    genre = max(changes, key=changes.get)
                    if changes[genre] >= .2 and new_genre[genre]-old_genre[genre] >= 5:
                        keys = sorted({r['track'] for r in newer if r['genre'] == genre and r['plays']})[:7]
                        add('genre_shift', 'genre:' + genre, f'More room for {genre}.',
                            f'{genre} went from {round(old_genre[genre]/old_count*100)}% to {round(new_genre[genre]/new_count*100)}% of your plays from one week to the next.',
                            keys=keys, since=older_start, until=week, anchor=week,
                            departure=changes[genre], filters={'overlay': 'genre'})

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
    if len(regulars) >= 5:
        add('adoption', 'cohort:' + shifted(today, -28), 'New songs you came back to.',
            f'You found these {len(regulars)} songs a few weeks ago and came back to each on at least two more days.',
            keys=regulars, visual='returns', stat=len(regulars), stat_label='songs played again',
            since=shifted(today, -28), strength=len(cohort)/15,
            filters={'track': regulars},
            anchor=hashlib.sha256('|'.join(sorted(cohort)).encode()).hexdigest()[:16])
    return findings
