"""Retry schedules, provider coverage, and configuration refresh decisions."""


def integration_retry(provider, previous, *, now, retry_after, error):
    failures = (previous['failures'] if previous else 0) + 1
    deadline = max(retry_after, now + min(3600, 30 * 2 ** min(failures - 1, 7)),
                   previous['retry_at'] if previous else 0)
    return dict(provider=provider, failures=failures, retry_at=deadline, error=error)


def artwork_retry(previous, outcome, now):
    failures = (previous['failures'] if previous else 0) + 1 if outcome.status == 'retry' else 0
    retry_at = now + max(outcome.retry_in, min(900, 60 * 2 ** min(failures - 1, 4))) if failures else 0
    return dict(status=outcome.status, failures=failures, retry_at=retry_at, error=outcome.error)


def coverage_status(configured, state, pending):
    if configured != 'available':
        return configured
    return state or ('pending' if pending else 'unknown')


def log_scope(field):
    return ('release' if field in ('year', 'label', 'mb_release_ids', 'cover_url') else
            'item' if field in ('popularity', 'gain', 'loudness', 'dynamic_complexity') else 'recording')


def identifiers(field, value, *, strict):
    if not value or field not in ('mb_recording_id', 'isrc'):
        return None, []
    supplied = value if isinstance(value, (list, tuple)) else [value]
    if strict and (len(supplied) > 1024 or any(not isinstance(identifier, str) or
            not 0 < len(identifier.encode()) <= 4096 for identifier in supplied)):
        raise ValueError('Identifier evidence requires bounded nonempty text')
    return 'musicbrainz' if field == 'mb_recording_id' else 'isrc', [str(identifier) for identifier in supplied]


def configuration_changes(configuration, inputs):
    changes = []
    for provider, current in configuration.items():
        previous = inputs['configuration'].get(provider)
        changed = previous is not None and (previous['fingerprint'] != current['fingerprint'] or
                  previous['status'] != 'available' and current['status'] == 'available')
        replace = previous is None or any(previous[key] != current[key] for key in ('fingerprint', 'status'))
        clear = current['status'] != 'available' and inputs['frontiers'].get(provider) is not None
        restart = current['status'] == 'available' and changed
        if replace or clear or restart:
            changes.append(dict(provider=provider, current=current, replace=replace, clear=clear, restart=restart))
    return changes


def refresh_page(rows, cutoff, *, runtime):
    stale, jobs = [], []
    for row in rows:
        if runtime:
            newer = row['at'] is not None and row['at'] * 1_000_000 > cutoff
            completed = newer and row['status'] != 'retry'
        else:
            newer = row['last_attempt'] > cutoff
            completed = row['completed_attempt'] > cutoff
        if row['at'] is not None and not newer:
            stale.append(row['id'])
        if row['first_enriched_at'] is not None and not completed:
            jobs.append(row['id'])
    return stale, jobs
