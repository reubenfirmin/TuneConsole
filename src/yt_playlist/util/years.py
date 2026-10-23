"""Year normalization shared by catalog summaries and recommendation policy."""


def decade(year, *, as_str=False):
    """Floor the first four year digits; unknown or malformed years stay unknown."""
    value = str(year or '')[:4]
    if not value.isdigit():
        return None
    result = int(value) // 10 * 10
    return str(result) if as_str else result
