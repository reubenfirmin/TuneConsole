"""Saved recipe validation and current relational projections over captured values."""
FIELDS = ('media', 'model', 'journey', 'opportunity_id', 'option_id')


def facets(value):
    if not isinstance(value, dict):
        raise ValueError('A saved playlist recipe requires an object')
    selections = {} if value.get('facets') is None else value['facets']
    if not isinstance(selections, dict):
        raise ValueError('Playlist recipe facets require an object')
    result = []
    for kind, field in (('genre', 'genres'), ('era', 'eras')):
        if field not in selections or selections[field] is None:
            continue
        if not isinstance(selections[field], list):
            raise ValueError('Playlist recipe facets require ordered lists')
        for position, label in enumerate(selections[field]):
            if not isinstance(label, str) or not label.strip():
                raise ValueError('Playlist recipe facets require nonempty labels')
            result.append((kind, position, kind + ':' + label, label))
    return result


def project(value, fields, facets):
    value.update({field: fields[field] for field in FIELDS if field in value})
    selections = {} if value.get('facets') is None else value['facets']
    for kind, field in (('genre', 'genres'), ('era', 'eras')):
        if field in selections and selections[field] is not None:
            selections[field] = [label for row_kind, label in facets if row_kind == kind]
    return value
