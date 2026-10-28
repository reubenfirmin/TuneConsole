"""One deterministic naming policy for versioned musical groups and their consumers."""
from collections import Counter

import numpy as np

from yt_playlist.rec.group_similarity import DIMENSIONS
from yt_playlist.util import genre_catalog

POLICY = 'supported-contrasting-names-1'
TRAITS = (('instrumental', 'Instrumental', 'Vocal'), ('groove', 'Danceable', None),
          ('relaxed', 'Relaxed', 'Driving'), ('acoustic', 'Acoustic', None),
          ('density', 'Dense', 'Spacious'), ('aggression', 'Aggressive', 'Gentle'),
          ('happiness', 'Bright', None), ('sadness', 'Melancholic', None))


def name_groups(result, recordings, *, previous=(), metadata=None):
    metadata = metadata or {}
    labels = genre_catalog.family_labels()
    prior = {item['group_id']: item for item in previous}
    names = {}
    for node in result['nodes']:
        members = [recordings[i] for i in node['rows']]
        genres = Counter(r.genre for r in members if r.genre)
        families = Counter(r.family for r in members if r.family)
        base = 'Mixed styles'
        for counts, label_map in ((families, labels), (genres, {})):
            if counts and counts.most_common(1)[0][1] >= .55 * len(members):
                value = counts.most_common(1)[0][0]
                base = label_map.get(value, value)
        traits = []
        medians = {}
        old = prior.get(node.get('group_id'), {})
        for dimension, high, low in TRAITS:
            dim = DIMENSIONS.index(dimension)
            measured = [r.values[dim] for r in members if r.support[dim] >= .3]
            if len(measured) < max(3, .6 * len(members)):
                continue
            median = float(np.median(measured))
            medians[dimension] = median
            retaining_high = old.get('name') == f'{high} {base.lower()}'
            retaining_low = low and old.get('name') == f'{low} {base.lower()}'
            for text, matches in ((high, lambda value: value >= (.60 if retaining_high else .65)),
                                  (low, lambda value: value <= (.40 if retaining_low else .35))):
                if text and sum(matches(value) for value in measured) >= .6 * len(members):
                    traits.append((dimension, text))
        retained = next((f'{text} {base.lower()}' for _dim, text in traits
                         if old.get('name') == f'{text} {base.lower()}'), None)
        artists = Counter(metadata.get(r.key, {}).get('artist') for r in members)
        artists.pop(None, None)
        artists.pop('', None)
        names[node['index']] = {'name': retained or base, 'base': base, 'traits': traits, 'medians': medians,
            'subtitle': ', '.join(name for name, _count in artists.most_common(3)),
            'genres': genres.most_common(4), 'families': families.most_common(4), 'display': True,
            'naming_policy': POLICY}
    siblings = {}
    for node in result['nodes']:
        siblings.setdefault(node['parent'], []).append(node['index'])
    for indices in siblings.values():
        for index in indices:
            item = names[index]
            collisions = [names[other] for other in indices if other != index and names[other]['base'] == item['base']]
            if not collisions:
                continue
            preferred = item['name']
            item['name'] = item['base']
            candidates = sorted(item['traits'], key=lambda trait: f'{trait[1]} {item["base"].lower()}' != preferred)
            for dimension, descriptor in candidates:
                retaining = f'{descriptor} {item["base"].lower()}' == preferred
                if all(dimension in other['medians'] and abs(item['medians'][dimension] - other['medians'][dimension]) >= (.15 if retaining else .2)
                       for other in collisions):
                    item['name'] = f"{descriptor} {item['base'].lower()}"
                    break
        counts = Counter(names[index]['name'].casefold() for index in indices)
        for index in indices:
            if counts[names[index]['name'].casefold()] > 1:
                names[index]['display'] = False
    if (result['nodes'] and not result['nodes'][0]['leaf'] and
            len(names[0]['families']) > 1):
        names[0].update(name='Your musical interests', display=False)
    return names
