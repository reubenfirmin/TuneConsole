"""Update decisions over supplied settings and host facts."""
CHECK_INTERVAL_S = 4 * 3600


def check_due(last, first_due, now, interval_s=CHECK_INTERVAL_S):
    try:
        return now >= float(first_due) if last is None and first_due is not None else (
            last is None or now - float(last) >= interval_s)
    except (TypeError, ValueError):
        return True


def version_tuple(version):
    parts = [int(part) for part in str(version).split('.')]
    return tuple((parts + [0, 0, 0])[:3])


def out_of_date(current, latest):
    try:
        return version_tuple(current) < version_tuple(latest)
    except (ValueError, TypeError):
        return False


def update_offer(current, latest, dismissed, kind):
    if kind == 'dev' or not latest or latest == dismissed or not out_of_date(current, latest):
        return None
    return {'current': current, 'latest': latest, 'kind': kind}
