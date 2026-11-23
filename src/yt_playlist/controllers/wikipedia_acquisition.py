"""Acquire bounded Wikipedia envelopes before applying detached subject policy."""
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.providers.base import LookupUnavailable


def summary(policy, search, fetch, kind, display, *, select=None):
    try:
        titles = list(dict.fromkeys(title for title in search(kind, display)
                                   if policy.matches_subject(kind, display, title)))
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as exc:
        raise LookupUnavailable('Wikipedia search unavailable') from exc
    failed = False
    candidates = {}
    for title in titles:
        try:
            result = policy.project_summary(fetch(title), kind, display, title)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception:
            failed = True
            continue
        if result:
            if select is None:
                return result
            candidates[result['title']] = result
    if failed:
        raise LookupUnavailable('Wikipedia summaries unavailable')
    if select is not None and candidates:
        try:
            return select(tuple(candidates.values()), len(titles) > 1)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as exc:
            raise LookupUnavailable('Wikipedia article identity unavailable') from exc
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
