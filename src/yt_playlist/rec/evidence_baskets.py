"""Co-membership rules for present-day and causal recommendation training."""
from yt_playlist.util.years import decade

ARTIST_CAP = 50
CONTENT_CAP = 80
EXCLUDED_VIDEO_TYPES = ('MUSIC_VIDEO_TYPE_UGC',)


def grouped(rows, eligible, *, cap=None):
    groups = {}
    for group, key in rows:
        if key in eligible:
            groups.setdefault(group, set()).add(key)
    return [list(keys) for keys in groups.values() if len(keys) > 1 and (cap is None or len(keys) <= cap)]


def content(rows, eligible, axes):
    families, years = [], []
    for row in rows:
        if row['key'] not in eligible:
            continue
        if row['genre']:
            families.append((axes[row['genre']][0], row['key']))
        year = decade(row['year'])
        if year is not None:
            years.append((year, row['key']))
    return grouped(families, eligible, cap=CONTENT_CAP) + grouped(years, eligible, cap=CONTENT_CAP)


def historical_coverage(*, eligible, safe_playlists, known_playlists, session_groups):
    return {'eligible_tracks': len(eligible), 'safe_playlists': len(safe_playlists),
            'known_playlists': known_playlists, 'session_baskets': session_groups,
            'mutable_content_baskets_excluded': True,
            'undated_relationship_baskets_excluded': ['album', 'artist']}
