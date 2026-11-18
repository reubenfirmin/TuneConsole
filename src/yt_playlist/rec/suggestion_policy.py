"""Suggestion feedback decisions over captured metadata and parameters."""


def feedback_axis(explicit, reason, *, family=None, decade=None, artist=None,
                  popularity=None, mainstream_min=0):
    if explicit and explicit.split(':', 1)[0] in ('genre', 'era', 'artist', 'pop'):
        return explicit
    value = {'vibe': family, 'era': decade, 'artist': artist}.get(reason)
    if value:
        return f"{ {'vibe': 'genre', 'era': 'era', 'artist': 'artist'}[reason]}:{value}"
    if reason == 'mainstream' and popularity is not None and popularity >= mainstream_min:
        return 'pop:mainstream'
    return None


def feedback_change(kind, reason, lane, now, lanes):
    return {'until': now + 14 * 86400 if kind == 'not_now' else None,
            'signed': 1.0 if kind == 'more' else -1.0,
            'lane': lane if reason != 'own_it' and lane in lanes else None,
            'factor': .85 if kind in ('dismiss', 'less', 'not_now') else 1.15 if kind == 'more' else None}


def mood_window(now, halflife_days):
    return now - 4 * halflife_days * 86400
