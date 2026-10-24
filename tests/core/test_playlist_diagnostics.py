"""Bounded continuation diagnostics preserve authoritative reads and uncertain writes."""
import json

import pytest

from tests.core.test_playlist_page_accounting import first, page, token
from tests.test_saved_album_snapshot import _production_client
from tests.radio_session_support import poc, play
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.playlist_snapshot import _page_envelope
from yt_playlist.library.playlist_snapshot import is_complete_playlist


@pytest.mark.parametrize('response,stage,clue', [
    ({}, 'continuation.envelope', 'object[0]'),
    ({'responseContext': {'PRIVATE_KEY': 'PRIVATE_SESSION'}}, 'continuation.envelope', 'responseContext'),
    ({'onResponseReceivedActions': []}, 'continuation.envelope', 'list[0]'),
    ({'onResponseReceivedEndpoints': [{'appendContinuationItemsAction': {'continuationItems': []}}]},
     'continuation.envelope', 'onResponseReceivedEndpoints'),
    (page(None), 'contents.type', 'null'),
    (page('PRIVATE_CONTENTS'), 'contents.type', 'string'),
    (page([None]), 'contents.item_type', 'item_index'),
    (page([{'PRIVATE_RENDERER': {'text': 'PRIVATE_TITLE'}}]), 'contents.renderer', 'object[1]'),
    (page([{'musicResponsiveListItemRenderer': {}}]), 'contents.parse', 'track'),
    (page([token('')]), 'contents.continuation', 'continuation'),
])
def test_page_two_explains_validation_stage_without_returning_payloads(response, stage, clue):
    client, _ = _production_client([first([token('PRIVATE_TOKEN')]), response])
    with pytest.raises(LibrarySnapshotError) as error:
        client.get_playlist('PL', limit=None)
    message = str(error.value)
    assert 'page=2' in message and f'"stage":"{stage}"' in message and clue in message
    assert 'PRIVATE_' not in message


def test_diagnostics_are_bounded_even_for_a_large_private_envelope():
    items = [{'PRIVATE_RENDERER': 'PRIVATE_TOKEN'} for _ in range(2000)]
    response = {'PRIVATE_KEY': 'PRIVATE_VALUE', 'onResponseReceivedActions': [
        {'appendContinuationItemsAction': {'continuationItems': items}}] * 2000}
    safe = _page_envelope(response)
    encoded = json.dumps(safe)
    assert len(encoded) < 4096 and 'PRIVATE_' not in encoded
    assert safe['onResponseReceivedActions'] == 'list[1000]'
    assert len(safe['onResponseReceivedActions.sample']) == 8
    assert safe['onResponseReceivedActions.sample'][0]['items'] == 'list[1000]'


def test_valid_terminal_empty_continuation_remains_authoritative():
    client, _ = _production_client([first([token('next')]), page([])])
    result = client.get_playlist('PL', limit=None)
    assert result['tracks'] == [] and is_complete_playlist(result)
    assert result['_readAudit']['pages'] == 2


def test_radio_retains_uncertain_append_and_readback_recovers_without_replaying(poc):
    controller, remote, *_ = poc
    controller.start()
    original = remote.get_playlist
    fail = [False]
    remote.after_add = lambda: fail.__setitem__(0, True)

    def read(*args, **kwargs):
        if fail[0]:
            fail[0] = False
            client, _ = _production_client([first([token('PRIVATE_TOKEN')]), page(None)])
            return client.get_playlist('PL', limit=None)
        return original(*args, **kwargs)

    remote.get_playlist = read
    with pytest.raises(LibrarySnapshotError, match='contents.type'):
        play(controller, 'v3')
    assert controller.session.pending['video_id'] == 'v4'
    assert controller.status()['ahead'] == 0
    assert remote.adds == [['v4']]
    play(controller, 'v3')
    assert controller.status()['ahead'] == 1
    assert remote.adds == [['v4']]
    assert not controller.session.halted
