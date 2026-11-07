"""Polling policy needs no catalog; actual startup recovery has two HTTP owners."""
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from yt_playlist.rec import home_prepared
from yt_playlist.rec.home_cards import CardsResult
from yt_playlist.web.routes.home_views import HomeViews


@pytest.mark.parametrize('attempt,next_attempt,interval', [
    (0, 1, 3), (10, 10, 30), (999, 10, 30), ('invalid', 1, 3),
])
def test_pending_polling_is_bounded_and_ready_offers_stop_it(
        monkeypatch, attempt, next_attempt, interval):
    def forbidden(*args, **kwargs):
        pytest.fail('Polling policy opened a database')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    context = SimpleNamespace(store=SimpleNamespace(_read_only=True), templates=None,
                              logger=logging.getLogger(__name__), rec_worker=None, vibe_requests=None)
    view = HomeViews(context)
    result = CardsResult(protos=[{'label': 'Usable offer'}], slots=[], genre_notice=None,
                         genre_warning=None, genre_options=[], requested_genres=[],
                         epoch=0, vibe=None, preparing=True)
    cards = Mock(return_value=result)
    monkeypatch.setattr(home_prepared, 'load', Mock(return_value=None))
    monkeypatch.setattr(home_prepared, 'cards', cards)
    request = Request({'type': 'http', 'method': 'GET', 'path': '/home/cards',
                       'headers': [],
                       'query_string': f'prepare_attempt={attempt}'.encode()})
    waiting = view.card_context(request, 1000)
    assert waiting['prepare_attempt'] == next_attempt
    assert waiting['prepare_interval'] == interval
    assert waiting['protos'] == result.protos

    result.preparing = False
    ready = view.card_context(request, 1001)
    assert ready['prepare_attempt'] is None
    assert ready['protos'] == result.protos
    assert cards.call_count == 2
