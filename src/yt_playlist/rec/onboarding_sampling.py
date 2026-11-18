"""Genre-diverse owned-track sampling over acquired rows and explicit randomness."""


def sample_tracks(rows, n, rng):
    by_family = {}
    for row in rows:
        by_family.setdefault(row['genre'] or '', []).append(row)
    for family in by_family.values():
        rng.shuffle(family)
    families, result = list(by_family.values()), []
    rng.shuffle(families)
    index = 0
    while len(result) < n and any(families):
        family = families[index % len(families)]
        if family:
            row = family.pop()
            result.append({'video_id': row['vid'], 'title': row['title'], 'artist': row['artist'],
                'album': row['album'] or '', 'thumbnail': row['thumb'], 'key': row['k'],
                'duration': row['duration']})
        index += 1
    return result
