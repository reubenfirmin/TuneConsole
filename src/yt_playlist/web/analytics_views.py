"""Analytics presentation over supplied facts, without acquisition."""
from yt_playlist.rec.ticker import candle_geometry, ticker_rows
from yt_playlist.util.navigation import album_url, artist_url, playlist_url


def more_albums(discography, collection, identities=None):
    browse_ids = {a.get('browse') for a in collection if a.get('browse')}
    identities = identities or {}
    represented = {identities[bid] for bid in browse_ids if identities.get(bid) is not None}
    result = []
    for album in discography:
        bid = album.get('browse_id')
        aid = identities.get(bid)
        if bid in browse_ids or aid is not None and aid in represented:
            continue
        result.append(dict(album))
        if bid:
            browse_ids.add(bid)
        if aid is not None:
            represented.add(aid)
    return result


def album_year(album):
    year = str(album.get('year') or '')[:4]
    return int(year) if year.isdigit() else 0


def ticker(facts, dim):
    data = ticker_rows(facts['corpus'], facts['windows'])

    def link(cat):
        if dim == 'artist':
            return artist_url(cat.removeprefix('@artist:'))
        if dim == 'album' and cat in facts['browse']:
            return album_url(facts['browse'][cat])
        if dim == 'playlist' and cat in facts['playlists']:
            return playlist_url(facts['playlists'][cat])

    return {'axis_max': data['axis_max'], 'rows': [
        {**row, 'geo': candle_geometry(row, data['axis_max']), 'link': link(row['cat']),
         'thumbnail': facts['thumbnails'].get(row['cat']),
         'label': facts['labels'].get(row['cat'], row['cat'])}
        for row in [r for r in data['rows'] if r['high'] > 0][:100]]}


def rankings(facts):
    return {'songs': facts['songs'], 'tickers': {
        dim: {**ticker(inputs, dim), 'close_days': facts['close_days'], 'span_days': facts['span_days'],
              'compare': dim in ('genre', 'year', 'artist')}
        for dim, inputs in facts['tickers'].items()}}


def artist(facts):
    info = facts['info']
    additional = more_albums((info or {}).get('albums', []), facts['collection'], facts['identities'])
    additional = [{**a, 'saved': a.get('browse_id') in facts['saved_ids']} for a in additional]
    albums = sorted([*facts['collection'], *[
        {'album': a['title'], 'browse': a.get('browse_id'), 'year': a.get('year'),
         'thumb': a.get('thumbnail'), 'saved': a['saved'], 'songs': 0, 'n_pls': 0, 'plays': 0}
        for a in additional]], key=album_year, reverse=True)
    return {key: facts[key] for key in ('artist', 'songs', 'collection', 'saved_owners', 'info')} | {
        'yt_albums': additional, 'albums': albums, 'total_plays': sum(s['plays'] for s in facts['songs'])}
