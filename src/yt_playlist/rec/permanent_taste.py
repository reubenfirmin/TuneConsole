"""Prepare a durable baseline from independent, dated listening support."""
from datetime import date

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.listening_calendar import day_at
from yt_playlist.core.rec_model_policy import CHOICE_SUPPORT_SURFACE, PERMANENT_SURFACE, PERMANENT_WORK
from yt_playlist.rec import layers, transient

POLICY = 'dated-episode-support-v5'
PAGE_SIZE = 512
MIN_DAYS = 3
MAX_DAYS = 16
PLAY_SUPPORT = .25
DELIBERATE_SUPPORT = 1.


def add_support(units, day, episode, amount):
    """Keep at most one vote per listening day and per episode for each facet."""
    for known_day, known in units.items():
        if known[0] == episode:
            units[known_day] = [episode, max(known[1], amount)]
            return
    if day not in units or units[day][1] < amount:
        units[day] = [episode, amount]
    if len(units) > MAX_DAYS:
        # Preserve the range of dated support and prioritize deliberate evidence.
        oldest, latest = min(units), max(units)
        remove = min((day for day in units if day not in (oldest, latest)),
                     key=lambda day: (units[day][1], day))
        del units[remove]


def reading(units):
    support = sum(unit[1] for unit in units.values())
    days = len(units)
    span = (date.fromisoformat(max(units)) - date.fromisoformat(min(units))).days if days else 0
    deliberate = sum(unit[1] >= DELIBERATE_SUPPORT for unit in units.values())
    ready = days >= MIN_DAYS and deliberate and support >= 1.5
    return {'days': days, 'span_days': span, 'support': support, 'deliberate_days': deliberate,
            'last_day': max(units) if units else None,
            'progress': min(1., days / MIN_DAYS, support / 1.5, deliberate),
            'weight': 1. + .8 * support / (support + 4.) if ready else 1.}


def _collect(reader, work, page_size):
    from yt_playlist.library.listening_window import advance_playback

    cutoff = work['until']
    plays = reader.history.taste_replay_page(work['after'], cutoff, limit=page_size)
    ratings = reader.history.taste_rating_page(work['ratings_after'], cutoff, limit=page_size)
    page = [(row, False) for row in plays] + [(row, True) for row in ratings]
    page.sort(key=lambda item: (item[0]['played_at'], item[1], item[0]['id']))
    # Evidence pages can span months. Read backward only along continuous activity.
    sessions = {account: layers.PlaybackSession(account, *state)
                for account, state in work['sessions'].items()}
    excluded_lists = set(transient.radio_list_ids(reader))
    quarantined = reader.rec_eligibility.generated_only_keys()
    votes = []
    before = max(work['after'][0], work['ratings_after'][0])
    for row, deliberate in page[:page_size]:
        check_cancellable_work()
        at, account, key = row['played_at'], str(row['account_id']), row['identity_key']
        if at > before:
            advance_playback(reader, sessions, before, at)
        before = at
        if deliberate:
            work['ratings_after'] = [at, row['id']]
            day = day_at(at, row['timezone'])
            session = sessions.get(account)
            episode = session.episode if session and at < session.expires_at else f'like:{account}:{day}'
            votes.append((key, day, episode, DELIBERATE_SUPPORT))
        else:
            work['after'] = [at, row['id']]
            session = layers.advance_session(sessions, {'account_id': account, 'at': at})
            if key not in quarantined and row['playlist_ytm_id'] not in excluded_lists:
                votes.append((key, row['day'], session.episode, PLAY_SUPPORT))
    work['sessions'] = {account: [state.started_at, state.last_at] for account, state in sessions.items()}
    axes_for = {}
    for axis, keys in transient.facets_for(reader, (key for key, *_ in votes)).items():
        for key in keys:
            axes_for.setdefault(key, []).append(axis)
    for key, day, episode, amount in votes:
        for axis in axes_for.get(key, ()):
            add_support(work['facets'].setdefault(axis, {}), day, episode, amount)
    return len(page) >= page_size


def _inputs(reader, through):
    content, evidence = reader.prepared_content.counters()
    total, covered, next_play = reader.history.taste_play_extent(through)
    # Each new play adds one row and one evidence revision. Subtract the insertions
    # so the revision still detects edits and deletions, even when a play changes date.
    # Keep the number of plays through the cutoff to detect late historical inserts.
    version = [POLICY, content, evidence - total, covered, reader.history.taste_rating_revision(),
               (reader.rec_proposals.get_proposals(CHOICE_SUPPORT_SURFACE) or {}).get('version')]
    return version, next_play


def _merge_choices(reader, facets):
    projection = reader.rec_proposals.get_proposals(CHOICE_SUPPORT_SURFACE) or {}
    mixes = projection.get('mixes', {})
    for descriptor_id, votes in mixes.items():
        descriptor = reader.rec_ledger.mix_descriptor(int(descriptor_id))
        if not descriptor:
            continue
        axes = reader.genres.feature_axes(descriptor['genres'])
        for axis in {f'genre:{axis}' for tokens in axes.values() for axis in tokens if axis}:
            units = facets.setdefault(axis, {})
            for day, (episode, strength) in votes.items():
                add_support(units, day, episode, strength)
    confirmed_tracks = projection.get('tracks', {})
    keys = list(confirmed_tracks)
    for offset in range(0, len(keys), PAGE_SIZE):
        check_cancellable_work()
        for axis, matches in transient.facets_for(reader, keys[offset:offset + PAGE_SIZE]).items():
            units = facets.setdefault(axis, {})
            for key in matches:
                for day, (episode, strength) in confirmed_tracks[key].items():
                    add_support(units, day, episode, strength)


def _current(reader, document, now):
    if document.get('policy') != POLICY:
        return False
    version, next_play = _inputs(reader, document['through'])
    next_at = document.get('next_at')
    return (document.get('inputs') == version and (next_at is None or now < next_at)
            and (next_play is None or now < next_play)
            and not reader.player_events.activity_added_before(*document['playback']))


def _checkpoint(work):
    return tuple(work.get(key) for key in ('inputs', 'until', 'after', 'ratings_after', 'playback'))


def prepare(store, now, *, page_size=PAGE_SIZE):
    """Resume one page, then atomically replace the baseline after a consistent replay."""
    with store.read_view(pinned=True) as reader:
        published = reader.rec_proposals.get_proposals(PERMANENT_SURFACE) or {}
        if _current(reader, published, now):
            return False
        work = reader.rec_proposals.get_proposals(PERMANENT_WORK) or {}
        expected = _checkpoint(work)
        cutoff = work.get('until', now)
        version, _ = _inputs(reader, cutoff)
        if (work.get('inputs') != version
                or reader.player_events.activity_added_before(*work['playback'])):
            if cutoff != now:
                version, _ = _inputs(reader, now)
            work = {'inputs': version, 'until': now, 'facets': {}, 'sessions': {},
                    'after': [-1, -1], 'ratings_after': [-1, -1]}
        player_event_id = reader.player_events.latest_id()
        more = _collect(reader, work, page_size)
        # Only activity at or before the processed evidence can invalidate this page.
        # Later player frames will be visible when a subsequent page reaches their time.
        work['playback'] = [player_event_id, max(work['after'][0], work['ratings_after'][0])]
        completed = {}
        if not more:
            _merge_choices(reader, work['facets'])
            completed = {axis: reading(units) for axis, units in work['facets'].items()}
            next_like = reader.history.next_taste_like(work['until'])
    with atomic_write(store):
        latest_version, next_play = _inputs(store, work['until'])
        if latest_version != version or store.player_events.activity_added_before(*work['playback']):
            return True
        previous = store.rec_proposals.get_proposals(PERMANENT_SURFACE) or {}
        if previous != published:
            return not _current(store, previous, now)
        latest = store.rec_proposals.get_proposals(PERMANENT_WORK) or {}
        if _checkpoint(latest) != expected:
            return True
        if more:
            store.rec_proposals.put_proposals(PERMANENT_WORK, work, now)
            return True

        next_at = min((at for at in (next_play, next_like) if at is not None), default=None)
        weights = {axis: row['weight'] for axis, row in completed.items() if row['weight'] != 1.}
        store.rec_proposals.put_proposals(PERMANENT_SURFACE, {
            'inputs': version, 'policy': POLICY, 'through': work['until'], 'next_at': next_at,
            'weights': weights, 'facets': completed, 'playback': work['playback']}, now)
        for axis in sorted(previous.get('weights', {}).keys() | weights.keys()):
            old, new = previous.get('weights', {}).get(axis, 1.), weights.get(axis, 1.)
            if old != new:
                store.rec_signals.log_graduation(axis, 'dated-support',
                    completed.get(axis, {}).get('support', 0.), new / old, new, now)
        store.rec_proposals.put_proposals(PERMANENT_WORK, {}, now)
    return next_at is not None and next_at <= now
