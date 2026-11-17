"""Acquire verified release envelopes; editorial interpretation uses supplied facts."""
import re

from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded, check_cancellable_work


def fetch(policy, album, *, search, recording, credential, releases, master, release):
    result = policy.empty()
    if not album.get('title') or not album.get('artist'):
        return result
    linked = None
    try:
        check_cancellable_work()
        matches = [row for row in search(album).get('release-groups') or [] if policy._mb_matches(row, album)]
        if matches:
            identity = matches[0].get('id', '')
            if re.fullmatch(r'[0-9a-f-]{36}', identity):
                check_cancellable_work()
                full = recording(identity)
                projected, linked = policy.project_musicbrainz(full, album, identity)
                result.update(projected)
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        result['retry'] = True
    token = credential()
    try:
        if linked is None and token:
            check_cancellable_work()
            linked = policy.select_discogs(releases(album, token), album)
        if linked and linked[1].isdigit():
            kind, identity = linked
            check_cancellable_work()
            if kind == 'master':
                identity = str(master(identity, token).get('main_release') or '')
            if identity.isdigit():
                check_cancellable_work()
                result = policy.project_discogs(result, release(identity, token), album, identity)
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        result['retry'] = True
    return result
