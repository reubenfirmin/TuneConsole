"""Known undated windows stay truthful and quiet; new formats stay diagnosable."""
import logging
from types import SimpleNamespace

from yt_playlist.repos import history


def test_repeated_weekly_scans_are_summarized_and_parse_each_bucket_once(store, monkeypatch, caplog):
    identity = store.identities.upsert_identity('main', 'credentials', None, True)
    rows = [(f'this-{i}|artist', 'This week') for i in range(54)]
    rows += [(f'last-{i}|artist', 'Last week') for i in range(13)]
    rows += [('dated|artist', 'Yesterday')]
    parsed = []
    parse = history._parse_played_date

    def counted(*args):
        parsed.append(args[0])
        return parse(*args)

    monkeypatch.setattr(history, '_parse_played_date', counted)
    caplog.set_level(logging.INFO, logger=history.__name__)
    for _ in range(2):
        store.history.record_history_plays(identity, 10 * 86400, rows, source_timezone='UTC')
    assert parsed == ['This week', 'Last week', 'Yesterday'] * 2
    assert len(caplog.records) == 2
    assert all(r.levelno == logging.INFO for r in caplog.records)
    assert all(r.args[1:3] == (67, {'last week': 13, 'this week': 54}) for r in caplog.records)
    assert store.history.listening_day_counts() == {'dated|artist': 1}
    assert store.history.recent_plays_with_ts() == [('dated|artist', 9 * 86400 + 43200)]
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 0
    observations = store.conn.execute('SELECT source,precision FROM listening_observations').fetchall()
    assert [tuple(row) for row in observations] == [('ytm_history', 'calendar_day')]


def test_unknown_categories_warn_once_and_again_after_cooldown(store, monkeypatch, caplog):
    identity = store.identities.upsert_identity('main', 'credentials', None, True)
    clock = [100.0]
    monkeypatch.setattr(history, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    caplog.set_level(logging.DEBUG, logger=history.__name__)
    rows = [('unknown|artist', 'vor 2 Tagen')] * 50
    store.history.record_history_plays(identity, 86400, rows)
    store.history.record_history_plays(identity, 2 * 86400, rows)
    store.history.record_history_plays(identity, 3 * 86400, [('new|artist', '25 de junio')])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert warnings[0].args[1:3] == (50, {'vor 2 tagen': 50})
    assert warnings[1].args[2] == {'25 de junio': 1}
    clock[0] += 901
    store.history.record_history_plays(identity, 4 * 86400, rows)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3
    assert len([r for r in caplog.records if r.levelno == logging.DEBUG]) == 4
    assert store.history.listening_day_counts() == {}


def test_many_unknown_formats_have_bounded_warning_payload_and_memory(store, caplog):
    identity = store.identities.upsert_identity('main', 'credentials', None, True)
    caplog.set_level(logging.WARNING, logger=history.__name__)
    store.history.record_history_plays(identity, 86400,
                                      [(f'{i}|artist', f'unknown {i}') for i in range(500)])
    assert len(caplog.records) == 1
    assert caplog.records[0].args[1] == 500
    assert len(caplog.records[0].args[2]) == 8
    assert caplog.records[0].args[3] == 492
    assert len(store.history._bucket_warnings) == 128
    assert store.history.listening_day_counts() == {}
