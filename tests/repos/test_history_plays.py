"""#49/#58 capture-time play counting: plays are keyed by (identity_key, played-DATE), so a re-fetched
window (even relabeled Today->Yesterday) never inflates, and same-date repeats merge."""
import datetime
import time

import pytest

from yt_playlist.core.store import Store
from yt_playlist.repos.history import _parse_played_date

DAY = 86400


@pytest.fixture(autouse=True)
def utc_machine_timezone(monkeypatch):
    """Exercise browser timezones independently of the server's timezone."""
    with monkeypatch.context() as env:
        env.setenv("TZ", "UTC")
        time.tzset()
        try:
            yield
        finally:
            env.undo()
            time.tzset()


def _ts(day):
    return day * DAY + 50000          # some moment within `day`


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def _counts(s):
    return s.history.listening_day_counts()


# --- _parse_played_date ---

def test_parse_today_and_yesterday():
    assert _parse_played_date("Today", _ts(100)) == 100 * DAY + 43200
    assert _parse_played_date("Yesterday", _ts(100)) == 99 * DAY + 43200


def test_parse_missing_bucket_is_undateable():
    """Was: missing -> sync day. That fallback re-stamped YouTube's whole lingering window as "played
    today" on every sync. A row we cannot date is not a play today; it is a row we cannot date."""
    assert _parse_played_date(None, _ts(100)) is None
    assert _parse_played_date("", _ts(100)) is None
    assert _parse_played_date("   ", _ts(100)) is None


def test_parse_relabel_resolves_to_same_date():
    # the SAME play: "Today" on day 100, "Yesterday" on day 101 -> identical absolute date
    assert _parse_played_date("Today", _ts(100)) == _parse_played_date("Yesterday", _ts(101))


def test_parse_explicit_date_string():
    sync = int(datetime.datetime(2026, 6, 27, tzinfo=datetime.timezone.utc).timestamp())
    want = (datetime.date(2026, 6, 25) - datetime.date(1970, 1, 1)).days
    assert _parse_played_date("Jun 25", sync) == want * DAY + 43200


def test_parse_unparseable_is_undateable_not_today():
    """A localized month name must not be silently attributed to the sync day: that mis-dated every
    non-English user's entire recently-played window, every sync."""
    assert _parse_played_date("hoy (localized)", _ts(100)) is None
    assert _parse_played_date("vor 2 Tagen", _ts(100)) is None
    assert _parse_played_date("25 de junio", _ts(100)) is None


@pytest.mark.parametrize("zone,first,second,first_label,second_label,new_plays", [
    # UTC midnight is still the same local day, including Yesterday's lingering window.
    ("America/Costa_Rica", "2026-10-20T23:30", "2026-10-21T00:30", "Today", "Today", 0),
    ("America/Costa_Rica", "2026-10-20T23:30", "2026-10-21T00:30", "Yesterday", "Yesterday", 0),
    # Local midnight starts a new day; relabeling yesterday's play must still merge.
    ("America/Costa_Rica", "2026-10-21T05:30", "2026-10-21T06:30", "Today", "Today", 1),
    ("America/Costa_Rica", "2026-10-21T05:30", "2026-10-21T06:30", "Today", "Yesterday", 0),
    ("Pacific/Kiritimati", "2026-10-20T23:30", "2026-10-21T00:30", "Today", "Today", 0),
    ("Pacific/Kiritimati", "2026-10-20T09:30", "2026-10-20T10:30", "Today", "Today", 1),
    ("Pacific/Kiritimati", "2026-10-20T09:30", "2026-10-20T10:30", "Today", "Yesterday", 0),
    # Both sides of DST changes remain on the same local calendar date.
    ("America/New_York", "2026-11-01T05:30", "2026-11-01T06:30", "Today", "Today", 0),
    ("America/New_York", "2026-03-08T06:30", "2026-03-08T07:30", "Today", "Today", 0),
])
def test_browser_calendar_day_dedup(zone, first, second, first_label, second_label, new_plays):
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    first_ts = datetime.datetime.fromisoformat(first + "+00:00").timestamp()
    second_ts = datetime.datetime.fromisoformat(second + "+00:00").timestamp()
    assert s.history.record_history_plays(iid, first_ts, [("song|artist", first_label)],
                                  source_timezone=zone) == 1
    assert s.history.record_history_plays(iid, second_ts, [("song|artist", second_label)],
                                  source_timezone=zone) == new_plays
    assert _counts(s) == {"song|artist": 1 + new_plays}


@pytest.mark.parametrize("zone,capture,expected", [
    ("America/Costa_Rica", "2026-01-01T00:30", "2025-01-01T12:00"),
    ("Pacific/Kiritimati", "2025-12-31T12:30", "2026-01-01T12:00"),
])
def test_yearless_date_uses_browser_year(zone, capture, expected):
    taken_at = datetime.datetime.fromisoformat(capture + "+00:00").timestamp()
    want = datetime.datetime.fromisoformat(expected + "+00:00").timestamp()
    assert _parse_played_date("Jan 1", taken_at, zone) == want


def test_legacy_clients_use_machine_local_date(monkeypatch):
    with monkeypatch.context() as env:
        env.setenv("TZ", "America/Costa_Rica")
        time.tzset()
        first = datetime.datetime(2026, 10, 20, 23, 30, tzinfo=datetime.timezone.utc).timestamp()
        assert _parse_played_date("Today", first) == _parse_played_date("Today", first + 3600)


def test_live_key_and_history_bucket_share_browser_date():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    taken_at = datetime.datetime(2026, 10, 21, 0, 30, tzinfo=datetime.timezone.utc).timestamp()
    assert s.history.record_history_plays(iid, taken_at, ["song|artist"],
                                  source_timezone="America/Costa_Rica") == 1
    assert s.history.record_history_plays(iid, taken_at + 3600, [("song|artist", "Today")],
                                  source_timezone="America/Costa_Rica") == 0
    want = datetime.datetime(2026, 10, 20, 12, tzinfo=datetime.timezone.utc).timestamp()
    assert s.history.recent_plays_with_ts() == [("song|artist", want)]


# --- record_history_plays ---

def test_dedups_by_played_date_across_relabel():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), [("a|x", "Today")]) == 1
    assert s.history.record_history_plays(iid, _ts(101), [("a|x", "Yesterday")]) == 0   # same date -> no inflation
    assert s.history.record_history_plays(iid, _ts(101), [("b|y", "Today")]) == 1       # genuinely new
    assert _counts(s) == {"a|x": 1, "b|y": 1}


def test_merges_same_date_repeats():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), [("a|x", "Today"), ("a|x", "Today")]) == 1


def test_accepts_bare_keys_backward_compat():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), ["a|x", "b|x"]) == 2     # bare key -> sync day
    assert s.history.record_history_plays(iid, _ts(100), ["a|x"]) == 0            # same day, idempotent


def test_reset_play_history():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    s.history.record_history_plays(iid, _ts(100), [("a|x", "Today")])
    s.history.reset_play_history(iid)
    assert _counts(s) == {}


# --- undateable window rows must not become phantom plays -----------------------------------------
# get_history() returns YouTube's whole recently-played window (hundreds of rows) on EVERY sync. The
# old code stamped any row it could not date as "played today", so the same play was re-recorded daily.
# Measured on a real database: one sync wrote 230 history rows, 153 of which were tracks whose actual
# plays all happened on other days. It also misfired for every non-English locale at once, because only
# English month names are parsed.

def test_a_window_row_with_no_played_bucket_is_dropped():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), [("a|x", "Today"), ("b|y", None)]) == 1
    assert _counts(s) == {"a|x": 1}


def test_a_window_row_with_an_empty_played_bucket_is_dropped():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), [("a|x", "  ")]) == 0
    assert _counts(s) == {}


def test_a_localized_played_bucket_is_dropped_not_stamped_today():
    """A German or Spanish YouTube locale must not silently record its whole window as today's plays."""
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), [("a|x", "vor 2 Tagen"), ("b|y", "25 de junio")]) == 0
    assert _counts(s) == {}


def test_a_dateable_row_still_backfills():
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), [("a|x", "Yesterday")]) == 1
    assert _counts(s) == {"a|x": 1}


def test_repeated_syncs_of_a_lingering_undateable_window_record_nothing():
    """The exact phantom mechanism: the same undateable window seen on five consecutive syncs."""
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    for d in range(100, 105):
        s.history.record_history_plays(iid, _ts(d), [("lingering|track", None)])
    assert _counts(s) == {}


def test_a_dated_row_is_recorded_once_across_repeated_syncs():
    """Contrast: a row YouTube CAN date is recorded once, however many times we see it."""
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    day100 = datetime.date(1970, 1, 1) + datetime.timedelta(days=100)     # the day _ts(100) falls in
    label = day100.strftime("%b %-d")                                     # e.g. "Apr 11"
    s.history.record_history_plays(iid, _ts(100), [("real|track", "Today")])
    for d in range(101, 105):
        s.history.record_history_plays(iid, _ts(d), [("real|track", label)])      # explicit date, same day
    assert _counts(s)["real|track"] == 1


def test_bare_string_keys_still_mean_the_sync_day():
    """live_plays passes a bare key because it KNOWS the play is happening now."""
    s = _store(); iid = s.identities.upsert_identity("m", "c", None, True)
    assert s.history.record_history_plays(iid, _ts(100), ["a|x"]) == 1
    assert _counts(s) == {"a|x": 1}
