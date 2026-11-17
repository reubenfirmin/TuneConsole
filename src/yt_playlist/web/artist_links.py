"""Artist navigation uses local credits or an explicit provider identity."""
from collections.abc import Mapping
from jinja2 import pass_context
from yt_playlist.util.navigation import artist_credit_links


def navigation(artists):
    @pass_context
    def links(context, subject, album=False):
        get = subject.get if isinstance(subject, Mapping) else lambda field: getattr(subject, field, None)
        values = dict(name=get('artist') or '', identity=get('artist_id'),
            browse=get('artist_browse_id') or get('artist_browse'),
            video=None if album else get('video_id') or get('videoId'),
            album=(get('browse') or get('browse_id') or get('album_browse')) if album else None,
            credits=tuple((row.get('name'), row.get('id') or row.get('browseId'))
                          for row in get('artists') or () if isinstance(row, Mapping) and row.get('name')))
        request = context.get('request')
        cache = None
        if request is not None:
            cache = getattr(request.state, 'artist_links', None)
            if cache is None:
                cache = request.state.artist_links = {}
        key = tuple(values.values())
        if cache is not None and key in cache:
            return cache[key]
        result = artist_credit_links(artists.navigation(**values))
        if cache is not None:
            cache[key] = result
        return result

    return links


def radio_navigation(state, artists):
    """Attach exact credits to the bounded, currently proposed Radio songs."""
    if not state.get('directions'):
        return state
    links, cache = navigation(artists), {}

    def song(value):
        key = value.get('video_id'), value.get('artist')
        if key not in cache:
            cache[key] = links({}, value)
        return {**value, 'artist_links': cache[key]}

    return {**state, 'directions': [{**branch, 'tracks': [song(value) for value in branch['tracks']]}
                                   for branch in state['directions']]}
