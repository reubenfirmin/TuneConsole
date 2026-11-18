"""Pure owner-selection rule behind client_for: no SQLite, explicit facts in."""
from types import SimpleNamespace

import pytest

from yt_playlist.core.accounts import client_for, select_account_id, select_remaining_master
from yt_playlist.core.action_errors import ActionInputError


def account(id, is_master=False):
    return SimpleNamespace(id=id, is_master=is_master)


def playlist(account_id):
    return SimpleNamespace(account_id=account_id)


def test_playlist_owner_always_wins_with_no_explicit_account():
    assert select_account_id(playlist=playlist(7), accounts=[account(1, True), account(7)]) == 7


def test_playlist_owner_wins_even_when_it_matches_an_explicit_account():
    assert select_account_id(playlist=playlist(7), accounts=[account(7)], account_id=7) == 7


def test_explicit_account_disagreeing_with_the_playlist_owner_is_rejected():
    with pytest.raises(ActionInputError):
        select_account_id(playlist=playlist(7), accounts=[account(7), account(9)], account_id=9)


def test_explicit_account_is_used_as_is_without_a_playlist():
    assert select_account_id(accounts=[account(1, True), account(9)], account_id=9) == 9


def test_default_falls_back_to_the_master_account():
    assert select_account_id(accounts=[account(1), account(2, True)]) == 2


def test_missing_master_is_rejected_rather_than_guessed():
    with pytest.raises(ActionInputError):
        select_account_id(accounts=[account(1), account(2)])


def test_client_for_rejects_a_missing_recorded_owner_instead_of_falling_back_to_main():
    store = SimpleNamespace(
        playlists=SimpleNamespace(get_playlist=lambda pid: playlist(99)),
        accounts=SimpleNamespace(get_accounts=lambda: [account(1, True)]))
    with pytest.raises(ActionInputError) as excinfo:
        client_for(store, {1: 'main-client'}, playlist_id=42)
    assert excinfo.value.code == 'account_disconnected'


def test_remaining_master_prefers_the_account_still_flagged_as_master():
    assert select_remaining_master([account(1), account(2, True), account(3)]).id == 2


def test_remaining_master_falls_back_to_the_first_remaining_account():
    assert select_remaining_master([account(5), account(6)]).id == 5
