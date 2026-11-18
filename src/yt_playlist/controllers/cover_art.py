"""Bounded cover acquisition; source envelopes are projected by the provider policy."""
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.util import net


def front_cover(release_ids, fetch, *, maximum=4):
    collected = []
    for identity in list(dict.fromkeys(release_ids or []))[:maximum]:
        with net.probe_errors() as errors:
            try:
                url = fetch(identity)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if not any(error is exc for error in errors):
                    net.record_probe_failure(exc)
                url = None
        collected.extend(errors)
        if url:
            for error in errors:
                net.record_probe_failure(error)
            return url
    for error in collected:
        net.record_probe_failure(error)
    return None


def lookup(release_ids, fetch, now, result, error_retry):
    with net.probe_errors() as errors:
        try:
            url = fetch(release_ids)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as exc:
            net.record_probe_failure(exc)
            url = None
    if errors:
        at = now()
        delay = max([error_retry, *(getattr(error, 'retry_in', 0) for error in errors),
                     *(net.retry_after(error, at) - at for error in errors)])
        return result('retry', url, retry_in=delay, error=str(errors[-1]))
    return result('found', url) if url else result('absent')
