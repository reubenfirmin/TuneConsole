"""#83 adaptive holdout: a 6-day library gets a 2-day holdout instead of a silent None at 30."""
from tests.repository_mock_support import no_database, repository_store
from yt_playlist.rec.eval_recs import best_holdout

DAY = 86400.0


def test_thin_history_gets_small_holdout(no_database):
    store = repository_store(rec_evidence={
        'evaluation_history_bounds': (100 * DAY + 50000, 106 * DAY + 50000)})
    assert best_holdout(store) == 2
    store.rec_evidence.evaluation_history_bounds.assert_called_once_with()


def test_deep_history_caps_at_30(no_database):
    store = repository_store(rec_evidence={
        'evaluation_history_bounds': (100 * DAY + 50000, 465 * DAY + 50000)})
    assert best_holdout(store) == 30


def test_empty_history_floors_at_1(no_database):
    store = repository_store(rec_evidence={'evaluation_history_bounds': (None, None)})
    assert best_holdout(store) == 1
