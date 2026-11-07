"""Known undated windows stay truthful and quiet; new formats stay diagnosable."""
import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers import history
from yt_playlist.repos.history import HistoryRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def listening_history():
    repo = create_autospec(HistoryRepo, instance=True, spec_set=True)
    repo.calendar_facts.return_value = {'timezone': 'UTC', 'confirmed': 1}
    repo.day_count.return_value = 0
    repo.subject_facts.return_value = {'direct': None, 'exact': None, 'aliases': []}
    return history.History(repo, None, None, lambda: set(), nullcontext, Mock())


def test_repeated_weekly_scans_are_summarized_and_parse_each_bucket_once(listening_history, monkeypatch, caplog):
    identity = 3
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
        listening_history.record_history_plays(identity, 10 * 86400, rows, source_timezone='UTC')
    assert parsed == ['This week', 'Last week', 'Yesterday'] * 2
    assert len(caplog.records) == 2
    assert all(r.levelno == logging.INFO for r in caplog.records)
    assert all(r.args[1:3] == (67, {'last week': 13, 'this week': 54}) for r in caplog.records)
    assert listening_history.repository.record_days.call_count == 2
    listening_history.repository.record_days.assert_called_with(identity, 10 * 86400,
        [('dated|artist', 'dated|artist', None, '1970-01-10', '1970-01-10', 'ytm_history')], 'UTC')


def test_unknown_categories_warn_once_and_again_after_cooldown(listening_history, monkeypatch, caplog):
    identity = 3
    clock = [100.0]
    monkeypatch.setattr(history, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    caplog.set_level(logging.DEBUG, logger=history.__name__)
    rows = [('unknown|artist', 'vor 2 Tagen')] * 50
    listening_history.record_history_plays(identity, 86400, rows)
    listening_history.record_history_plays(identity, 2 * 86400, rows)
    listening_history.record_history_plays(identity, 3 * 86400, [('new|artist', '25 de junio')])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert warnings[0].args[1:3] == (50, {'vor 2 tagen': 50})
    assert warnings[1].args[2] == {'25 de junio': 1}
    clock[0] += 901
    listening_history.record_history_plays(identity, 4 * 86400, rows)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3
    assert len([r for r in caplog.records if r.levelno == logging.DEBUG]) == 4
    assert all(call.args[2] == [] for call in listening_history.repository.record_days.call_args_list)


def test_many_unknown_formats_have_bounded_warning_payload_and_memory(listening_history, caplog):
    identity = 3
    caplog.set_level(logging.WARNING, logger=history.__name__)
    listening_history.record_history_plays(identity, 86400,
                                      [(f'{i}|artist', f'unknown {i}') for i in range(500)])
    assert len(caplog.records) == 1
    assert caplog.records[0].args[1] == 500
    assert len(caplog.records[0].args[2]) == 8
    assert caplog.records[0].args[3] == 492
    assert len(listening_history._bucket_warnings) == 128
    listening_history.repository.record_days.assert_called_once_with(identity, 86400, [], 'UTC')
