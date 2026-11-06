"""Cheap source revision fingerprints shared by summary workers and readers."""
from yt_playlist.controllers.summary_inputs import DEPENDENCIES, SURFACES


def inputs(store, kind, now):
    return input_fingerprints(store, (kind,), now)[kind]


def input_fingerprints(store, kinds, now):
    """Combine detached storage facts with the summaries' refresh clocks."""
    facts, zone = store.summary_inputs.capture(kinds)
    if 'analytics' in kinds:
        from yt_playlist.rec.listening_dates import edition_day, timezone_name
        editorial_clock = (edition_day(now, timezone_name(zone)), int(now // 21600))
    return {kind: (revisions, external,
        editorial_clock if kind == 'analytics' else int(now // 86400) if kind in {'trends', 'model_health'} else None,
        present) for kind, (revisions, external, present) in facts.items()}
