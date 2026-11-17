"""History attribution and provenance decisions over supplied library matches."""
from collections import Counter


def import_identity(identities):
    main = [identity for identity in identities if identity.is_master]
    candidates = main or identities
    return candidates[0].id if len(candidates) == 1 else None


def spotify_rows(parsed, owned, matches):
    matched, unmatched, listens = [], Counter(), []
    for row in parsed:
        key = matches[(row['title'], row['artist'])]
        listens.append((row['spotify_uri'], row['ts'], row['artist'], key if key in owned else None))
        if key not in owned:
            unmatched[row['artist']] += 1
        else:
            # Spotify ids never become YouTube video ids.
            matched.append((key, None, row['ts']))
    return matched, unmatched, listens


def takeout_rows(parsed, owned, videos, recordings):
    matched, unmatched = [], Counter()
    for row in parsed:
        key = (videos.get(row['video_id']) if row['video_id']
               else recordings[(row['title'], row['artist'])])
        if key not in owned:
            unmatched[row['artist'] or '?'] += 1
        else:
            matched.append((key, row['video_id'], row['ts']))
    return matched, unmatched


def discovery_seeds(unmatched, existing, min_plays):
    return [(artist, float(plays), artist in existing) for artist, plays in unmatched.items()
            if plays >= min_plays]
