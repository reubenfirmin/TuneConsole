"""Read playback continuity and local-day boundaries from listener evidence."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.listening import machine_timezone
from yt_playlist.rec import layers


def active_session(store, now):
    return store.memoized_read(('active-playback-session', now), lambda: _active_session(store, now))


def _playback_activity(store, start, end):
    return store.history.live_play_activity(start, end) + store.player_events.playback_activity(start, end)


def advance_playback(store, sessions, before, at):
    lookback = 2 * layers.SESSION_IDLE_S
    while True:
        start = max(before, at - lookback)
        activity = _playback_activity(store, start, at)
        # Apply same-time closes after the listening evidence, including across pages.
        activity = [row for row in activity if row['at'] != at or row.get('kind') != 'bye']
        if start == before or all(session.started_at >= start + layers.SESSION_IDLE_S
                                  for session in layers.playback_sessions(activity, at)):
            break
        lookback *= 2
    for row in sorted(activity, key=layers.playback_order):
        if row['at'] > before or row.get('kind') == 'bye':
            layers.advance_session(sessions, {**row, 'account_id': str(row['account_id'])})


def choice_playback_episodes(store, moments):
    if not moments:
        return {}
    episodes, sessions = {}, {}
    before = -float('inf')
    with store.read_view() as reader:
        for account, at in sorted(moments, key=lambda moment: moment[1]):
            check_cancellable_work()
            if at > before:
                advance_playback(reader, sessions, before, at)
                before = at
            session = sessions.get(str(account))
            if session and at < session.expires_at:
                episodes[account, at] = session.episode
    return episodes


def first_steering_session(store, at, until):
    def find():
        activity = _playback_activity(store, at, until)
        return layers.first_session(activity, at + layers.SESSION_IDLE_S)

    return store.memoized_read(('first-steering-session', at, until), find)


def _active_session(store, now):
    lookback = 2 * layers.SESSION_IDLE_S
    while True:
        start = now - lookback
        session = layers.active_session(_playback_activity(store, start, now), now)
        if session is None or session.started_at >= start + layers.SESSION_IDLE_S:
            return session
        lookback *= 2


def listening_window(store, now):
    return store.memoized_read(('listener-window', now), lambda: _listening_window(store, now))


def _listening_window(store, now):
    calendars = {None: machine_timezone(), **store.history.listening_calendars()}
    days = {account: layers.calendar_window(now, name) for account, name in calendars.items()}
    return layers.ListeningWindow(now, active_session(store, now), days)


def choice_timezone(store):
    calendars = set(store.history.listening_calendars().values())
    return next(iter(calendars)) if len(calendars) == 1 else machine_timezone()
