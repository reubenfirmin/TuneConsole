"""Acquire bounded Wikipedia envelopes before applying detached subject policy."""
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.providers.base import LookupUnavailable


def summary(policy, search, fetch, kind, display):
    try:
        titles = search(kind, display)
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as exc:
        raise LookupUnavailable('Wikipedia search unavailable') from exc
    failed = False
    for title in titles:
        if not policy.matches_subject(kind, display, title):
            continue
        try:
            result = policy.project_summary(fetch(title), kind, display, title)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception:
            failed = True
            continue
        if result:
            return result
    if failed:
        raise LookupUnavailable('Wikipedia summaries unavailable')
    return None


def artwork(policy, search, fetch, image_info, display):
    try:
        for title in search('artist', display):
            if not policy.matches_subject('artist', display, title):
                continue
            filename = policy.artwork_filename(fetch(title), display, title)
            if filename:
                result = policy.project_artwork(image_info(filename), display)
                if result:
                    return result
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as exc:
        raise LookupUnavailable('Wikipedia artwork unavailable') from exc
    return None
