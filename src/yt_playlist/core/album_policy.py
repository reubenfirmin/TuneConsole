"""Authority and credit rules for observations of an exact release."""


def metadata(current, values, rank):
    result = {}
    for field, value in values.items():
        if field == 'year' and value is not None:
            value = str(value)
        value = None if value == '' else value
        previous = current.get(field)
        result[field] = (value if value is not None else previous) if rank >= current.get('metadata_rank', 0) else (
            previous if previous is not None else value)
    result['metadata_rank'] = max(current.get('metadata_rank', 0), rank)
    return result


def replace_credits(existing, previous_rank, rank, *, credits, artist_id, artist):
    return bool((credits or artist_id is not None or artist) and (not existing or (
        (credits or artist_id is not None) and (previous_rank is None or rank >= previous_rank))))
