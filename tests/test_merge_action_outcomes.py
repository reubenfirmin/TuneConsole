"""Internal merge/legacy JSON failures cannot become validation messages."""
import json

import pytest

from tests.quiet_validation_support import DIAGNOSTIC
from tests.test_quiet_action_validation import quiet_actions


@pytest.mark.parametrize('kind', ['json', 'value'])
@pytest.mark.parametrize('operation', ['merge', 'legacy-delete'])
def test_playlist_lookup_diagnostic_is_logged_without_changing_draft_or_library(
        quiet_actions, monkeypatch, kind, operation, caplog):
    case, store = quiet_actions, quiet_actions['store']
    ids = f"{case['source']},{case['destination']}"
    assert case['http'].get('/merge?ids=' + ids).status_code == 200
    playlists, actions = store.playlists.get_playlists(), store.actions.get_actions()

    def fail(*args, **kwargs):
        if kind == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)
        raise ValueError(DIAGNOSTIC)

    with monkeypatch.context() as failing:
        failing.setattr(store.playlists, 'get_playlist', fail)
        if operation == 'merge':
            response = case['http'].post('/merge/apply?ids=' + ids)
            assert response.content == b'' and response.headers['hx-reswap'] == 'none'
        else:
            response = case['http'].post('/dupe/delete', data={
                'source': case['source'], 'target': case['destination']})
            assert response.json() == {'ok': False}
    assert response.status_code == 503
    assert DIAGNOSTIC not in response.text and 'toast-err' not in response.text
    assert DIAGNOSTIC in caplog.text
    assert store.playlists.get_playlists() == playlists and store.actions.get_actions() == actions
    assert case['http'].get('/merge?ids=' + ids).status_code == 200
    for provider in (case['main'], case['other']):
        assert not provider.deleted and not provider.added and not provider.created
