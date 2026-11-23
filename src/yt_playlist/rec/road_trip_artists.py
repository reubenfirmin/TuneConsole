"""Use recording credits or exact provider references for selected artists."""


def recipe_inputs(recipe):
    refs = recipe.get('artist_refs')
    if not refs:
        return {'artists': list(recipe.get('artists') or []), 'genres': list(recipe.get('genres') or [])}
    return {'artists': list(refs), 'artist_refs': refs, 'genres': list(recipe.get('genres') or [])}


def artist_item(key, refs):
    if key.startswith('@artist:') and key in refs:
        ref = refs[key]
        if not ref.get('browse_id'):
            return None
        return {'kind': 'artist', 'name': ref['name'], 'browse_id': ref['browse_id'], 'artist_key': key}
    return {'kind': 'artist', 'name': key}


def selected_artist(row, artists, refs, *, include_origin=False):
    """Return the selected artist key for a track, including a legacy name-only choice."""
    credits = row.get('artist_ids') or ()
    browse = row.get('artist_browse_id')
    name = (row.get('artist') or '').strip().casefold()
    origin = (row.get('input') or '').strip().casefold() if include_origin else None
    for key in artists:
        if key.startswith('@artist:') and key in refs:
            if key in credits or (browse and browse == refs[key].get('browse_id')):
                return key
        elif key and key.strip().casefold() in (name, origin):
            return key
    return None
