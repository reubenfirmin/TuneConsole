"""Malformed bounded reads must be rejected before consulting SQLite."""
import sqlite3
import threading

import pytest

from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo


class NoDatabase:
    _read_only = True

    def __init__(self):
        self._lock = threading.RLock()

    @property
    def conn(self):
        pytest.fail('Invalid metadata page accessed a connection')


@pytest.mark.parametrize('options', [{'limit': 0}, {'limit': 513}, {'limit': True}, {'after': -1},
                                   {'after': True}, {'mode_key': 1}])
def test_metadata_page_rejects_unbounded_or_ambiguous_inputs(monkeypatch, options):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid metadata page opened SQLite')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    repository = RecommendationPublicationsRepo(NoDatabase())
    with pytest.raises(ValueError, match='bounded recommendation metadata'):
        repository.recording_metadata_rows('home', **options)
