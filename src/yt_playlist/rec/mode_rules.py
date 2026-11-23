"""Framing and detached candidate rules shared by Home and pool commands."""
from yt_playlist.rec import artist_variety
from yt_playlist.rec.mode_selection import (PROTO_SIZE, CARD_LABELS as _CARD_LABELS,
                                          BAND_LABELS as _BAND_LABELS)

CARD_SURFACES = ('wheelhouse', 'explore', 'comfort', 'fresh')


def bundle_metadata(store, *, generation=None):
    row = store.recommendations.framing('home', generation=generation)
    if row is None:
        return {}
    result = {field: row[field] for bit, field in
              ((0, 'comfort_pool'), (2, 'full_pools'), (3, 'unmodeled'), (4, 'musical_generation'))
              if row['presence'] & (1 << bit)}
    if row['presence'] & 2:
        result['year_cuts'] = ([row['year_low'], row['year_high']]
            if row['year_low'] is not None or row['year_high'] is not None else None)
    return result


def _item_dict(it):
    return {'key': getattr(it, 'key', ''), 'video_id': it.video_id, 'title': it.title,
        'artist': it.artist, 'album': getattr(it, 'album', ''), 'thumbnail': it.thumbnail,
        'plays': getattr(it, 'plays', 0), 'reason': getattr(it, 'reason', ''),
        'lane': getattr(it, 'lane', ''), 'genre': getattr(it, 'genre', '') or '',
        'year': getattr(it, 'year', None), 'score': getattr(it, 'score', None)}


def _temporal_item(key, meta, genres, years):
    m = meta.get(key, {})
    return {'key': key, 'video_id': m.get('video_id'), 'title': m.get('title') or '',
        'artist': m.get('artist') or '', 'album': m.get('album') or '',
        'artist_ids': m.get('artist_ids', []),
        'thumbnail': m.get('thumbnail'), 'plays': 0, 'reason': '', 'lane': 'temporal',
        'genre': genres.get(key) or '', 'year': years.get(key)}


def _diversify(items, max_artist, max_album, *, limit=None):
    return artist_variety.select(items, max_artist, max_album, limit=limit)


def _in_band(year, band, lo, hi):
    if year is None:
        return False
    if band == 0:
        return year <= lo
    if band == 1:
        return lo < year <= hi
    return year > hi
