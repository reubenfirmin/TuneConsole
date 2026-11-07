from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers import wikipedia_cards
from yt_playlist.controllers.wikipedia_cards import WikipediaCards
from yt_playlist.repos.wiki import WikiRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def cards():
    return WikipediaCards(create_autospec(WikiRepo, instance=True, spec_set=True))


@pytest.mark.parametrize('found,days', [(True, 30), (False, 7)])
def test_freshness_boundary(cards, found, days):
    row = {'found': found, 'fetched_at': 1000}
    assert cards.is_fresh(row, 1000 + days * 86400 - 1)
    assert not cards.is_fresh(row, 1000 + days * 86400)


def test_failure_is_not_a_negative_answer_and_preserves_content(cards):
    subject = 'artist:offline'
    row = {'found': True, 'fetched_at': 0, 'extract': 'Old card.'}
    now = 31 * 86400
    cards.defer(subject, now, retry_at=now + 600)
    assert not cards.should_refresh(subject, row, now + 599)
    assert cards.should_refresh(subject, row, now + 600)
    assert row['fetched_at'] == 0
    cards.repository.put.assert_not_called()


def test_failure_without_content_retries_after_one_minute(cards):
    cards.defer('artist:offline', 1000)
    assert not cards.should_refresh('artist:offline', None, 1059)
    assert cards.should_refresh('artist:offline', None, 1060)
    cards.repository.put.assert_not_called()


@pytest.mark.parametrize('payload,found', [(None, False), ({'extract': 'A band.'}, True)])
def test_confirmed_answer_persists_explicit_decision_and_clears_backoff(cards, payload, found):
    cards.defer('artist:band', 1000)
    cards.put('artist:band', 'artist', 'Band', payload, 1001)
    cards.repository.put.assert_called_once_with(
        'artist:band', 'artist', 'Band', payload or {}, 1001, found=found, limit=512)
    assert cards.should_refresh('artist:band', None, 1001)


def test_failed_write_does_not_clear_existing_backoff(cards):
    cards.defer('artist:band', 1000)
    cards.repository.put.side_effect = OSError('write interrupted')
    with pytest.raises(OSError):
        cards.put('artist:band', 'artist', 'Band', {'extract': 'A band.'}, 1001)
    assert not cards.should_refresh('artist:band', None, 1001)


def test_backoffs_are_bounded_and_successful_answers_release_them(cards, monkeypatch):
    monkeypatch.setattr(wikipedia_cards, 'WIKI_CACHE_LIMIT', 3)
    for i in range(5):
        cards.defer(f'artist:{i}', i)
    assert cards.should_refresh('artist:0', None, 5)
    assert not cards.should_refresh('artist:4', None, 5)
    cards.put('artist:4', 'artist', '4', None, 5)
    assert cards.should_refresh('artist:4', None, 5)
    assert len(cards._retry_after) == 2
