"""Radio replacement recovery retains the selected branch and its offered menu."""
import json

import pytest

from tests.store_fixture_support import numbered_store
from yt_playlist.core import radio_storage
from yt_playlist.core.store import Store
from yt_playlist.web.radio_persistence import RadioPersistence


def seed(store, version, *, append=False):
    assert version == 107
    account = store.accounts.upsert_account('Radio', 'radio.json', None, True)
    state = {'version': 2, 'episode_id': 'episode', 'identity': account,
        'phase': 'appending' if append else 'replacing', 'playlist': 'RADIO',
        'tracks': [], 'position': 0, 'pending': {'video_id': 'next'} if append else None,
        'pending_sent': append, 'pending_started_at': 0 if append else None,
        'pending_result': {'status': 'UNKNOWN', 'videoId': 'next', 'setVideoId': 'handle'} if append else None,
        'replacement': None if append else {'allowed': [[], ['same', 'same'], []],
            'confirmed': ['same', 'same'], 'waiting': [], 'waiting_at': 0,
            'receipt': {'status': 'UNKNOWN', 'videoId': 'next', 'setVideoId': 'handle'},
            'adding': {'video_id': 'next'}, 'proposed': [{'video_id': 'next'}],
            'play_prefix': None, 'slot': 0, 'choice_id': 'choice', 'excluded': ['excluded']}}
    with store.conn:
        radio_storage.save(store.conn, state, expected=None, now=3)
    return ('tv_native', 'radio_native'), ('TV', 'episode'), {}, state


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Checkpoint persistence does not construct an HTTP application."""


def test_page_snapshot_does_not_adopt_a_newer_checkpoint_write_token(file_backed_store):
    store = file_backed_store
    _, _, _, state = seed(store, 107, append=True)
    persistence = RadioPersistence(store.radio_checkpoints)
    original_token = persistence.revision
    newer = {**state, 'error': 'Another worker owns this checkpoint'}
    with store.conn:
        next_token = radio_storage.save(store.conn, newer, expected=original_token, now=4)
    with store.read_view() as reader:
        assert persistence.view(reader) == {**newer, 'halted': False, 'stopped': False}
    assert persistence.revision == original_token
    with pytest.raises(ValueError, match='changed.*worker'):
        persistence._write(json.dumps({**state, 'error': 'Stale worker'}))
    assert radio_storage.load(store.conn) == (newer, next_token)


def test_append_receipt_retains_provider_fields_after_restart(file_backed_store):
    store = file_backed_store
    _, _, _, state = seed(store, 107, append=True)
    state['pending_result']['provider_details'] = {'attempt': 'original', 'optional': None}
    token = radio_storage.load_published(store.conn)[1]
    with store.conn:
        radio_storage.save(store.conn, state, expected=token, now=4)
    assert radio_storage.load(store.conn)[0] == state
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert radio_storage.load_published(reopened.conn)[0] == state
        assert radio_storage.load(reopened.conn)[0] == state
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()

def publish_choice(store, version, choice, seeded):
    """Publish an actual captured checkpoint below its restart/upgrade consumer."""
    assert version == 107
    _, _, _, state = seeded
    state['replacement']['choice_id'] = choice
    token = radio_storage.load_published(store.conn)[1]
    with store.conn:
        radio_storage.save(store.conn, state, expected=token, now=4)
    row = store.conn.execute('SELECT * FROM radio_operations').fetchone()
    assert radio_storage.load_published(store.conn)[0] == state

    return state, row


@pytest.mark.parametrize('numbered_store', [107], indirect=True)
def test_choice_and_menu_survive_checkpoint_publication_and_restart(numbered_store, tmp_path):
    store, version = numbered_store
    seeded = seed(store, version)
    for choice, branch in (({'id': 'branch', 'menu_id': 'offered-menu'}, 'branch'),
                           ('historical-choice', 'historical-choice'), (None, None)):
        state, row = publish_choice(store, version, choice, seeded)
        assert row['choice_id'] == branch
        assert radio_storage.load(store.conn)[0] == state
        reopened = Store(store._db_path)
        try:
            reopened.init_schema(defer_metadata=True)
            assert radio_storage.load(reopened.conn)[0] == state
            assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
        finally:
            reopened.conn.close()
