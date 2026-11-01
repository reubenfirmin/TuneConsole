"""Persist explicit recommendation references at the declared output boundaries."""
from . import recommendation_custody as publication

MAX_GENERATIONS = 128
MAX_TRACKS = 8192


def enabled(store):
    return getattr(store, '_recommendation_files', False)


def references(context):
    values = context.get('recommendation_generations', [])
    if (not isinstance(values, list) or len(values) > MAX_GENERATIONS
            or any(type(value) is not int or value < 1 for value in values)):
        raise ValueError('Invalid explicit recommendation publication references')
    return sorted(set(values))


def from_options(options):
    """Inspect the declared option/track boundary, never arbitrary nested JSON."""
    generations, count = set(), 0
    if not isinstance(options, list) or len(options) > 512:
        raise ValueError('Invalid recommendation options for publication retention')
    for option in options:
        tracks = option.get('tracks', []) if isinstance(option, dict) else []
        if not isinstance(tracks, list):
            raise ValueError('Invalid recommendation tracks for publication retention')
        count += len(tracks)
        if count > MAX_TRACKS:
            raise ValueError('Recommendation publication references exceed their capture budget')
        for track in tracks:
            context = track.get('serving_context') if isinstance(track, dict) else None
            if not isinstance(context, dict) or 'recommendation_generation_id' not in context:
                continue
            generation = context['recommendation_generation_id']
            if type(generation) is not int or generation < 1:
                raise ValueError('Invalid served recommendation publication')
            generations.add(generation)
    if len(generations) > MAX_GENERATIONS:
        raise ValueError('Recommendation publication references exceed their capture budget')
    return sorted(generations)


def retain(conn, kind, identity, generations):
    current = {row[0] for row in conn.execute('SELECT generation_id FROM recommendation_generation_refs '
                                             'WHERE kind=? AND external_key=?', (kind, identity))}
    if current and current != set(generations):
        raise ValueError('Recommendation evidence owner changed its immutable publication references')
    for generation in generations:
        publication.retain(conn, kind, identity, generation)


def prepared(conn, kind, surface, data, *, owner_prefix=None):
    identity = (owner_prefix or 'proposal:' + surface) + ':recommendations'
    options = data.get('protos', [])
    # Adaptive previews retain their generation explicitly, independently of
    # lane allocations which are not completed track menus.
    menus = [] if surface == 'adaptive_group_preview' else data.get('menus', [])
    if not isinstance(menus, list) or len(menus) > 128:
        raise ValueError('Invalid prepared recommendation menu references')
    held = data.get('menu')
    if isinstance(held, dict):
        menus = [*menus, held]
    generations = set(references(data))
    generations.update(from_options(options))
    for menu in menus:
        if isinstance(menu, dict):
            generations.update(from_options(menu.get('protos', [])))
    items = data.get('items', [])
    if not isinstance(items, list) or len(items) > 512:
        raise ValueError('Invalid prepared album recommendation references')
    generations.update(from_options([{'tracks': item.get('companion', [])}
                                     for item in items if isinstance(item, dict)]))
    generations.update(from_options([{'tracks': data.get(field, [])}
                                    for field in ('tracks', 'discovery_tracks', 'rediscover_tracks')]))
    generations = references({'recommendation_generations': sorted(generations)})
    publication.release(conn, kind, identity)
    retain(conn, kind, identity, generations)
