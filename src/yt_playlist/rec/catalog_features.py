"""Genre and era features derived from recorded catalog facts."""
from collections import Counter

from yt_playlist.util.years import decade

OWNED_WEIGHT = 1


def artist_features(rows):
    genres = Counter(row['genre'] for row in rows if row['genre'])
    years = [int(str(row['mb_year'])[:4]) for row in rows
             if str(row['mb_year'] or '')[:4].isdigit()]
    return {'genre': genres.most_common(1)[0][0] if genres else '',
            'year': min(years) if years else None}


def era_weights(rows):
    result = {}
    for row in rows:
        era = decade(row['y'], as_str=True)
        if era is not None:
            result[era] = result.get(era, 0) + row['w']
    return result


def play_weighted_facets(rows, plays):
    """Count each recording/facet pair once despite disagreeing upload metadata."""
    genres = {(row['identity_key'], row['genre']) for row in rows if row['genre']}
    years = {(row['identity_key'], row['mb_year']) for row in rows if row['mb_year']}
    genre_plays, era_plays = {}, {}
    for key, genre in sorted(genres, key=lambda pair: (str(pair[1]), pair[0])):
        genre_plays[genre] = genre_plays.get(genre, 0) + OWNED_WEIGHT + plays.get(key, 0)
    for key, year in sorted(years, key=lambda pair: (pair[0], str(pair[1]))):
        era = decade(year, as_str=True)
        if era is not None:
            era_plays[era] = era_plays.get(era, 0) + OWNED_WEIGHT + plays.get(key, 0)
    return {'genres': genre_plays, 'eras': era_plays}
