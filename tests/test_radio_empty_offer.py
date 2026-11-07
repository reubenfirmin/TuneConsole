"""Empty startup offers are ordinary state, not listening opportunities."""
import logging
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app
from yt_playlist.web.radio_listening import RadioListening


def test_repeated_fresh_account_seed_requests_have_no_measurement_or_saved_menu(store, caplog):
    identity = store.accounts.upsert_account('main', 'controlled-credential', None, True)
    app = create_app(store, lambda: {identity: FakeClient()}, background_work=False)
    with caplog.at_level(logging.WARNING, logger='yt_playlist.rec.choice_evidence'):
        with TestClient(app, base_url='http://127.0.0.1') as client:
            for _ in range(2):
                response = client.get('/radio/seeds')
                assert response.status_code == 200
                assert response.json() == {'tracks': [], 'menu_id': None}
    assert store.settings.get_setting('radio_seed_offer') is None
    assert store.rec_ledger.evidence().records('opportunity') == []
    assert store.rec_ledger.evidence().records('response') == []
    assert not [record for record in caplog.records
                if record.name == 'yt_playlist.rec.choice_evidence'
                and 'listening opportunity measurement failed' in record.getMessage()]


def test_empty_directions_do_not_record_a_visible_choice(store, caplog):
    listening = RadioListening(store, None, None, None, lambda: 10)
    session = SimpleNamespace(directions=SimpleNamespace(menu_id='empty', public=lambda _: []), seen_menus=set())
    listening.capture_menu(session)
    assert listening.seen(session, 'empty') == ({'ok': True}, ())
    assert not session.seen_menus
    assert not store.rec_ledger.evidence().records('opportunity')
    assert not store.rec_ledger.evidence().records('response')
    assert not caplog.records


@pytest.mark.parametrize('options', [[{'id': ''}], [{'id': 'same'}, {'id': 'same'}]])
def test_nonempty_invalid_choice_ids_still_fail_validation(store, options):
    from yt_playlist.rec import choice_evidence

    with pytest.raises(ValueError, match='distinct alternatives'):
        choice_evidence.issue(store, 'radio', options, 10, context={})
