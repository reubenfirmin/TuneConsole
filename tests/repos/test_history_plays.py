"""#49/#58 capture-time play counting: plays are keyed by (identity_key, played-DATE), so a re-fetched
window (even relabeled Today->Yesterday) never inflates, and same-date repeats merge."""
import datetime

from yt_playlist.core.store import Store
from yt_playlist.repos.history import _parse_played_date

DAY = 86400


def _ts(day):
    return day * DAY + 50000          # some moment within `day`


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def _counts(s):
    return {r[0]: r[1] for r in s.conn.execute(
        "SELECT identity_key, COUNT(*) FROM history_items GROUP BY identity_key")}


# --- _parse_played_date ---

def test_parse_today_yesterday_and_missing():
    assert _parse_played_date("Today", _ts(100)) == 100 * DAY + 43200
    assert _parse_played_date("Yesterday", _ts(100)) == 99 * DAY + 43200
    assert _parse_played_date(None, _ts(100)) == 100 * DAY + 43200          # missing -> sync day


def test_parse_relabel_resolves_to_same_date():
    # the SAME play: "Today" on day 100, "Yesterday" on day 101 -> identical absolute date
    assert _parse_played_date("Today", _ts(100)) == _parse_played_date("Yesterday", _ts(101))


def test_parse_explicit_date_string():
    sync = int(datetime.datetime(2026, 6, 27, tzinfo=datetime.timezone.utc).timestamp())
    want = (datetime.date(2026, 6, 25) - datetime.date(1970, 1, 1)).days
    assert _parse_played_date("Jun 25", sync) == want * DAY + 43200


def test_parse_unparseable_falls_back_to_sync_day():
    assert _parse_played_date("hoy (localized)", _ts(100)) == 100 * DAY + 43200


# --- record_history_plays ---

def test_dedups_by_played_date_across_relabel():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    assert s.record_history_plays(iid, _ts(100), [("a|x", "Today")]) == 1
    assert s.record_history_plays(iid, _ts(101), [("a|x", "Yesterday")]) == 0   # same date -> no inflation
    assert s.record_history_plays(iid, _ts(101), [("b|y", "Today")]) == 1       # genuinely new
    assert _counts(s) == {"a|x": 1, "b|y": 1}


def test_merges_same_date_repeats():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    assert s.record_history_plays(iid, _ts(100), [("a|x", "Today"), ("a|x", "Today")]) == 1


def test_accepts_bare_keys_backward_compat():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    assert s.record_history_plays(iid, _ts(100), ["a|x", "b|x"]) == 2     # bare key -> sync day
    assert s.record_history_plays(iid, _ts(100), ["a|x"]) == 0            # same day, idempotent


def test_reset_play_history():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    s.record_history_plays(iid, _ts(100), [("a|x", "Today")])
    s.reset_play_history(iid)
    assert _counts(s) == {}
