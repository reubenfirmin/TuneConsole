"""Held daily editions, selected using actual visible editorial exposure."""
import hashlib

from yt_playlist.rec.listening_dates import edition_day
from yt_playlist.rec.listening_insights import candidates

VERSION = 11


def present(edition):
    """Art direction holds for a reading, without tying artwork to a fixed slot."""
    stories = [{**story, 'backdrop': None, 'art_side': None} for story in edition['stories']]
    options = []
    for index, story in enumerate(stories):
        images = {story.get('art')}
        for row in story['timeline']:
            images.add(row.get('thumbnail'))
            images.update(song.get('thumbnail') for song in row.get('songs', {}).values())
        images.discard(None)
        images.discard('')
        if images:
            seed = hashlib.sha256((edition['day'] + story['id']).encode()).hexdigest()
            options.append((seed, index, sorted(images)))
    used = set()
    for seed, index, images in sorted(options)[:max(1, len(stories)//2)]:
        choices = [image for image in images if image not in used] or images
        image = choices[int(seed[:8], 16) % len(choices)]
        stories[index].update(backdrop=image, art_side='left' if int(seed[-1], 16)%2 else 'right')
        used.add(image)
    return {**edition, 'stories': stories}


def prepare(data, repo, now):
    key = edition_day(now, data['timezone'])
    previous = repo.prepared('listening_edition') or {}
    pool = candidates(data)
    valid = {item['id']: item for item in pool}
    prior = previous.get('stories', [])
    views = repo.impressions()
    recent_families = {s['family'] for s in pool if now-views.get(s['id'], -float('inf')) < 3*86400}
    recent_families.update(s['family'] for s in pool
                          if now-views.get('family:' + s['family'], -float('inf')) < 3*86400)
    fresh_theme = any(s['family'] not in recent_families and s['id'] not in views for s in pool)
    material = any(s['id'] not in {p['id'] for p in prior} and s['id'] not in views
                   and s['family'] in ('attachment', 'return', 'slow_heart') and s['strength'] >= .8
                   for s in pool)
    recent_subjects = set().union(*(set(s) for s in previous.get('recent_subjects', [])[:2]))
    for item in pool:
        novelty = 0 if item['id'] in views else 1
        item['score'] = .5*item['strength'] + .25*item['departure'] + .15*item['timeliness'] + .1*novelty
        item['score'] -= .1 * (item['subject'] in recent_subjects)
        item['tie'] = hashlib.sha256((key + item['id']).encode()).hexdigest()
    pool.sort(key=lambda item: (-item['score'], item['tie']))
    def topic(item):
        if item['subject'].startswith('artist:'):
            return 'artists'
        if item['family'] in ('discovery', 'adoption'):
            return 'discovery'
        if item['family'] in ('formats', 'format_shift'):
            return 'formats'
        if item['family'] in ('rating_collection', 'genre_shift'):
            return 'genres'
        return 'ratings'

    # Exposure influences the lead; it never removes useful supporting evidence.
    ordered = sorted(pool, key=lambda s: (
        -(s['score'] - .35*(s['family'] in recent_families)
          - .2*(now-views.get('hero:'+s['id'], -float('inf')) < 7*86400)), s['tie']))
    selected, families, subjects, topics = [], set(), set(), set()
    for distinct in (True, False):
        for item in ordered if distinct else pool:
            if (len(selected) == 4 or item['family'] in families or item['subject'] in subjects
                    or distinct and topic(item) in topics):
                continue
            selected.append(item)
            families.add(item['family'])
            subjects.add(item['subject'])
            topics.add(topic(item))

    prior_valid = (previous.get('timezone') == data['timezone'] and prior
                   and all(s['id'] in valid for s in prior))
    # A selector upgrade must evict panels that no longer meet evidence gates.
    # Stable prepared editions keep the current reading; the browser never swaps
    # an edition underneath a reader, even after publication of its successor.
    if prior_valid:
        holding = previous.get('day') == key and now-previous.get('built_at', now) < 21600
        if previous.get('version') == VERSION and (holding or
                len(prior) >= len(selected) and not (fresh_theme or material)):
            return previous if holding else present({**previous, 'day': key})
        alternatives = any(s['family'] not in {p['family'] for p in prior} for s in selected)
        if len(prior) >= len(selected) and not (material or fresh_theme and alternatives):
            selected = [valid[s['id']] for s in prior]
    selected = [{k: v for k, v in item.items() if k not in
                 ('score', 'tie', 'strength', 'departure', 'timeliness')} for item in selected]
    subjects = {s['subject'] for s in selected}
    identity = hashlib.sha256((key + '|'.join(s['id'] for s in selected)).encode()).hexdigest()[:24]
    return present({'id': identity, 'version': VERSION, 'day': key, 'built_at': now, 'timezone': data['timezone'],
            'stories': selected, 'available': bool(selected),
            'recent_subjects': [sorted(subjects), *previous.get('recent_subjects', [])[:1]]})
