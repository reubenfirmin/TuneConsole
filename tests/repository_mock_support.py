"""Explicit repository results for service tests that must not open SQLite."""
import sqlite3
from unittest.mock import Mock

import pytest


@pytest.fixture
def no_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('This service test must use repository inputs instead of SQLite')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)


def repository_store(*, values=None, **repositories):
    values = values or {}
    store = Mock(spec_set=[*repositories, *values])
    for name, operations in repositories.items():
        repo = Mock(spec_set=list(operations))
        for operation, result in operations.items():
            getattr(repo, operation).return_value = result
        setattr(store, name, repo)
    for name, value in values.items():
        setattr(store, name, value)
    return store


def transient_repository_store(*, values=None, **repositories):
    inputs = dict(
        settings={'get_setting': None},
        rec_signals={'recent_mood_events': []},
        history={'recent_plays_with_ts': [], 'play_events_since': [], 'listening_calendars': {},
                 'live_play_activity': [], 'recent_calendar_evidence': []},
        rec_model={'recent_liked_with_ts': [], 'disliked_with_ts': [],
                   'disliked_identity_keys': set(), 'get_rec_content_vectors': [],
                   'get_weights': {}, 'baseline_weights': {}, 'get_session_weights': {},
                   'get_lean': 1., 'get_leans': {}, 'recent_recommendation_feedback': [],
                   'content_generation': {'model': None, 'library': []}},
        player_events={'recent_skips_with_ts': [], 'playback_activity': []},
        rec_eligibility={'generated_only_keys': set()},
        playlists={'radio_playlist_ids': []},
        artists={'track_keys': {}},
        rec_query={'track_genres': {}, 'track_decades': {}, 'track_artists': {},
                   'track_popularity': {}},
        genres={'feature_axes': {'techno': ('techno', None), 'jazz': ('jazz', None)}},
        rec_proposals={'get_proposals': {}},
    )
    for name, operations in repositories.items():
        inputs[name] = {**inputs.get(name, {}), **operations}
    return repository_store(
        **inputs,
        values={'_read_only': True, 'transient_radio_playlists': set(),
                'memoized_read': Mock(side_effect=lambda key, calculate: calculate()),
                **(values or {})},
    )


@pytest.fixture
def transient_store(no_database):
    return transient_repository_store()
