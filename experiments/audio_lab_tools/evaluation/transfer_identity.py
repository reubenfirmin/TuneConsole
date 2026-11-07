"""Exclude connected supplied identities before any transfer model selection."""
import unicodedata

import numpy as np

from ..datasets.external_secondary import artist_keys, identity


def identity_keys(row, include_title=False):
    keys = {('artist', key) for key in artist_keys(row.get('artist') or '')}
    for field, kind in (('id', 'recording'), ('audio_sha256', 'audio')):
        if row.get(field):
            keys.add((kind, row[field]))
    if include_title and row.get('title'):
        title = unicodedata.normalize('NFKD', row['title'])
        keys.add(('title', identity(''.join(c for c in title if not unicodedata.combining(c)))))
    return keys


def exclusion(source, target):
    """Close artist/recording/audio matches through source collaboration chains.

    Titles remain conservative row exclusions, not component anchors. Declared
    groups/franchises are deliberately absent from this identity graph. Missing
    identities cannot join otherwise unrelated rows.
    """
    parents, first = list(range(len(source))), {}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    strong = [identity_keys(row) for row in source]
    for i, keys in enumerate(strong):
        for key in sorted(keys):
            if key in first:
                left, right = root(i), root(first[key])
                parents[max(left, right)] = min(left, right)
            else:
                first[key] = i
    components = [root(i) for i in range(len(source))]
    target_keys = set().union(*(identity_keys(row, True) for row in target))
    matches = [identity_keys(row, True) & target_keys for row in source]
    direct = np.array([bool(match) for match in matches], dtype=bool)
    anchored = {components[i] for i, keys in enumerate(strong) if keys & target_keys}
    excluded = np.array([direct[i] or component in anchored
                         for i, component in enumerate(components)], dtype=bool)
    audit = {
        'direct_excluded_ids': [r['id'] for i, r in enumerate(source) if direct[i]],
        'additional_excluded_ids': [r['id'] for i, r in enumerate(source) if excluded[i] and not direct[i]],
        'excluded_ids': [r['id'] for i, r in enumerate(source) if excluded[i]],
        'training_rows': int((~excluded).sum()),
        'anchored_components': [
            {'component': component,
             'anchors': [{'id': source[i]['id'], 'matches': sorted(strong[i] & target_keys)}
                         for i in range(len(source)) if components[i] == component and strong[i] & target_keys],
             'excluded_ids': [r['id'] for i, r in enumerate(source) if components[i] == component]}
            for component in sorted(anchored)],
        'missing_artist_rows': {
            'source': sum(not artist_keys(r.get('artist') or '') for r in source),
            'target': sum(not artist_keys(r.get('artist') or '') for r in target)},
    }
    return ~excluded, audit
