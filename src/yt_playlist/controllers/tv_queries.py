"""Capture a video playlist and its prepared artwork on one committed view."""


def playlist_items(reader, pid, *, artwork=True, images):
    playlist = reader.playlists.get_playlist(pid)
    recipe = reader.playlist_recipes.get_recipe(playlist.playlist_scope_id) if playlist else None
    if not isinstance(recipe, dict) or recipe.get('media') != 'video':
        return None
    metadata = {t['video_id']: t for t in recipe.get('videos', [])}
    tracks = reader.charts.playlist_tracks_detail(pid)
    items = [{**metadata.get(t['video_id'], {}), **t,
        'duration': t.get('duration') or metadata.get(t['video_id'], {}).get('duration', 0),
        'likeStatus': 'LIKE' if t.get('liked') else 'INDIFFERENT'}
        for t in tracks if t.get('video_id') and t.get('available') != 0]
    if artwork:
        art = {name: images(reader, name) for name in {t.get('source_artist') or t['artist'] for t in items}}
        for item in items:
            prepared_images, prepared = art[item.get('source_artist') or item['artist']]
            item.update(artist_art=prepared_images, artist_art_pending=not prepared)
    return playlist, items
