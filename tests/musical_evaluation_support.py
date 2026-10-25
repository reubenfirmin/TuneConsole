"""Offline evaluation artifacts, never listening labels or promotion evidence."""
from collections import Counter, defaultdict
import hashlib

from yt_playlist.rec import home_vibe, musical_character, musical_profiles
from yt_playlist.util.matching import normalize


def holdouts(rows, held_genres=(), *, salt='musical-listening-1'):
    """Keep connected artist/album credits together, including collaborations.

    Catalog identities still require review: unresolved aliases cannot become a
    claim of independently verified holdouts. Missing artist identity is excluded.
    """
    parent = {row['key']: row['key'] for row in rows}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    seen = {}
    for row in rows:
        artists = [('artist', str(value)) for value in row.get('artist_ids', [])]
        if not artists and normalize(row.get('artist') or ''):
            artists = [('credit', normalize(row['artist']))]
        albums = [('album', str(value)) for value in row.get('album_ids', [])]
        if not albums and normalize(row.get('album') or ''):
            # Conservative: repeated album titles may join unrelated releases,
            # reducing sample size rather than leaking a shared release.
            albums = [('album_title', normalize(row['album']))]
        for token in [*artists, *albums]:
            if token in seen:
                parent[root(row['key'])] = root(seen[token])
            else:
                seen[token] = row['key']
    groups = defaultdict(list)
    for row in rows:
        groups[root(row['key'])].append(row)
    result = {}
    held_genres = {normalize(name) for name in held_genres}
    for group in groups.values():
        group_id = musical_profiles.digest(sorted(row['key'] for row in group))
        if any(not row.get('artist_ids') and not normalize(row.get('artist') or '') for row in group):
            split = 'identity_review'
        elif any(normalize(row.get('genre') or '') in held_genres for row in group):
            split = 'genre_holdout'
        else:
            split = 'artist_album_holdout' if int(hashlib.sha256(
                (salt + group_id).encode()).hexdigest()[:8], 16) % 5 == 0 else 'development'
        result.update({row['key']: {'group': group_id, 'split': split} for row in group})
    return result


def coverage(profiles, keys):
    report = {name: Counter() for name in musical_profiles.DIMENSIONS}
    for key in keys:
        for name, counts in report.items():
            dimension = profiles.get(key, {}).get('dimensions', {}).get(name, {})
            value, support = musical_character.dimension({'musical': {'dimensions': {name: dimension}}}, name)
            counts['known' if value is not None else 'unknown'] += 1
            counts['supported_at_least_0.4'] += support >= .4
            counts['explicit'] += bool(dimension.get('override'))
            counts['conflicted'] += bool(dimension.get('conflict'))
            def kinds(value):
                found = {value['kind']} if value.get('kind') else set()
                return found | {kind for child in value.get('evidence', []) for kind in kinds(child)}
            for kind in kinds(dimension):
                counts['evidence:' + kind] += 1
    return {name: dict(counts) for name, counts in report.items()}


def listening_packet(rows, profiles, splits, *, per_extreme=15):
    """Sample predicted matches/counterexamples for blind, still-empty judgments.

    Selection strata/scores stay in a separate manifest. The listener packet has
    no predictions, scores, provider fields or expected answers.
    """
    candidates = [{**row, 'musical': musical_character.compact(profiles.get(row['key']))}
                  for row in rows if row.get('video_id') and splits[row['key']]['split'] != 'identity_review']
    candidates.sort(key=lambda row: row['key'])
    listener, manifest = [], []
    for preset in home_vibe.PRESETS:
        ordered = sorted(candidates, key=lambda row: home_vibe.order_key(row, preset))
        chosen = {}
        for stratum, pool in (('predicted_match', ordered), ('predicted_counterexample', list(reversed(ordered)))):
            groups, count = Counter(), 0
            for row in pool:
                group = splits[row['key']]['group']
                if groups[group] >= 2 or row['key'] in chosen:
                    continue
                chosen[row['key']] = row, stratum
                groups[group] += 1
                count += 1
                if count >= per_extreme:
                    break
        def strata(row):
            dimensions = (row['musical'] or {}).get('dimensions', {})
            known = [item for item in dimensions.values() if item.get('value') is not None]
            result = []
            if not known:
                result.append('missing')
            elif all(item.get('support', 0) < .4 for item in known):
                result.append('weak_only')
            if any(item.get('conflict') for item in known):
                result.append('conflicting')
            intensity = musical_character.intensity(row)
            bpm = row.get('bpm')
            if type(bpm) in (float, int) and ((bpm >= 150 and intensity < .4) or (bpm <= 80 and intensity > .6)):
                result.append('tempo_intensity_disagreement')
            instrumental, _ = musical_character.dimension(row, 'instrumental')
            density, _ = musical_character.dimension(row, 'density')
            if instrumental is not None and instrumental > .6 and (intensity > .6 or (density or 0) > .6):
                result.append('dense_instrumental')
            return result
        extras = Counter()
        for row in candidates:
            for stratum in strata(row):
                if row['key'] not in chosen and extras[stratum] < 2:
                    chosen[row['key']] = row, stratum
                    extras[stratum] += 1
        # Fixed pseudo-random presentation prevents best-first ordering from
        # revealing the expected answer. Repeated recordings retain one split.
        for key, (row, stratum) in sorted(chosen.items(), key=lambda pair:
                musical_profiles.digest([preset, pair[0], 'blind-order-1'])):
            identity = musical_profiles.digest([preset, key])[:20]
            listener.append({'id': identity, 'preset': preset, 'title': row['title'],
                'artist': row['artist'], 'url': 'https://music.youtube.com/watch?v=' + row['video_id'],
                'fit': None, 'intrusion': None, 'notes': ''})
            manifest.append({'id': identity, 'key': key, **splits[key], 'selection': stratum,
                'affinity': home_vibe.affinity(row, preset),
                'profile_version': (row['musical'] or {}).get('version'),
                'profile_digest': (row['musical'] or {}).get('digest')})
    pairs = []
    ordered = sorted(candidates, key=lambda row: musical_profiles.digest([row['key'], 'intensity-pairs-1']))
    for index in range(0, min(len(ordered) - 1, 40), 2):
        left, right = ordered[index:index + 2]
        pairs.append({'id': f'intensity-{index // 2}',
            'left': {field: left[field] for field in ('key', 'title', 'artist', 'video_id')},
            'right': {field: right[field] for field in ('key', 'title', 'artist', 'video_id')},
            'more_intense': None, 'notes': ''})
    return {'schema': 1, 'status': 'unlabelled', 'fit_values': ['fits', 'does_not_fit', 'unsure'],
            'pair_values': ['left', 'right', 'similar', 'unsure'],
            'instructions': 'Listen before judging. Leave uncertain or unheard examples unlabelled.',
            'tracks': listener, 'intensity_pairs': pairs}, manifest
