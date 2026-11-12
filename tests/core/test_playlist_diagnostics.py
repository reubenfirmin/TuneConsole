"""Bounded continuation diagnostics preserve authoritative reads and uncertain writes."""
import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.core.test_playlist_page_accounting import first, page, token
from tests.test_saved_album_snapshot import _production_client
from tests.radio_session_support import poc, play, radio_controller, seed_completed_pool
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, use_native_sql_calls,
)
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.playlist_snapshot import _page_envelope
from yt_playlist.library.playlist_snapshot import is_complete_playlist
from yt_playlist.web.radio_session import PlaylistRadio


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Parser/controller tests have no HTTP application lifecycle."""


@pytest.fixture(scope='module')
def diagnostic_radio_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def diagnostic_poc(tmp_path, diagnostic_radio_schema, monkeypatch):
    # Continuation recovery consumes these same 32 admitted catalog subjects.
    # Keep fresh real readers, thread admission and checkpoint publication in
    # the concurrent subject; input ingestion diagnostics are below it.
    with _file_consumer_store(tmp_path, diagnostic_radio_schema) as store:
        with monkeypatch.context() as inputs:
            use_native_sql_calls(inputs)
            pool = seed_completed_pool(store)
        with radio_controller(monkeypatch, store, pool) as state:
            yield state


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
    response = {'PRIVATE_KEY': 'PRIVATE_VALUE', 'alerts': [{'alertRenderer': {
        'type': 'PRIVATE_SEVERITY', 'text': {'runs': [{'text': 'PRIVATE_MESSAGE'}]}}}] * 2000,
        'onResponseReceivedActions': [
        {'appendContinuationItemsAction': {'continuationItems': items}}] * 2000}
    safe = _page_envelope(response)
    encoded = json.dumps(safe)
    assert len(encoded) < 4096 and 'PRIVATE_' not in encoded
    assert safe['onResponseReceivedActions'] == 'list[1000]'
    assert len(safe['onResponseReceivedActions.sample']) == 8
    assert safe['onResponseReceivedActions.sample'][0]['items'] == 'list[1000]'
    assert safe['alerts'] == 'list[1000]'
    assert safe['alert_sample'] == [{'renderer': 'alertRenderer', 'type': 'unknown',
                                    'message_class': 'unclassified'}] * 4


@pytest.mark.parametrize('response,classification,other_fields', [
    (None, 'non_object', None),
    ({}, 'empty_object', 0),
    ({'responseContext': {'PRIVATE_SESSION': 'PRIVATE_VALUE'},
      'trackingParams': 'PRIVATE_TOKEN'}, 'metadata_only', 0),
    ({'responseContext': {}, 'PRIVATE_KEY': 'PRIVATE_VALUE'}, 'unrecognized_envelope', 1),
    ({'frameworkUpdates': {'PRIVATE_KEY': 'PRIVATE_VALUE'}}, 'unrecognized_envelope', 0),
    ({'error': {'message': 'PRIVATE_MESSAGE', 'code': 503}}, 'provider_error', 0),
    (page(None), 'content_envelope', 0),
])
def test_continuation_envelope_categories_never_expose_provider_values(response, classification, other_fields):
    safe = _page_envelope(response)
    assert safe['classification'] == classification
    assert safe.get('other_fields') == other_fields
    assert 'PRIVATE_' not in json.dumps(safe)


@pytest.mark.parametrize('stop', [False, True])
def test_missing_page_two_after_append_preserves_checkpoint_and_controls(diagnostic_poc, monkeypatch, stop):
    controller, remote, frames, *_ = diagnostic_poc
    assert controller.start()['ok']
    original = remote.get_playlist
    remaining = [0]
    entered, release = threading.Event(), threading.Event()
    remote.after_add = lambda: remaining.__setitem__(0, 2)

    def read(*args, **kwargs):
        if remaining[0]:
            remaining[0] -= 1
            entered.set()
            assert release.wait(5), 'Incomplete readback was not released'
            client, _ = _production_client([first([token('PRIVATE_TOKEN')]), {
                'responseContext': {'mainAppWebResponseContext': {'loggedOut': False},
                                    'PRIVATE_SESSION': 'PRIVATE_VALUE', 'PRIVATE_CONTEXT': 'PRIVATE_VALUE'},
                'trackingParams': 'PRIVATE_TOKEN'}])
            return client.get_playlist('PL', limit=None)
        return original(*args, **kwargs)

    monkeypatch.setattr(remote, 'get_playlist', read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        advancing = pool.submit(play, controller, 'v3')
        try:
            assert entered.wait(3), 'The post-append read did not reach page two'
            assert pool.submit(controller.status).result(timeout=.5)['active']
            if stop:
                assert pool.submit(controller.stop).result(timeout=.5)['ok']
        finally:
            release.set()
        with pytest.raises(LibrarySnapshotError, match='continuation.envelope'):
            advancing.result(timeout=5)
    state = controller.persistence.load()
    assert state['pending']['video_id'] == 'v4' and state['pending_sent']
    assert state['pending_result']['status'] == 'STATUS_SUCCEEDED'
    assert [track['video_id'] for track in state['tracks']] == ['v1', 'v2', 'v3']
    assert remote.ids == ['v1', 'v2', 'v3', 'v4'] and remote.adds == [['v4']]
    if stop:
        assert state['stopped']
        controller.advance(controller.session)
        assert remote.adds == [['v4']] and len(frames) == 1
        return

    controller.suspend()
    controller._persist(controller.session)
    restored = PlaylistRadio(controller.ctx)
    controller.ctx.radio = restored
    assert restored.join()['active']
    # An incomplete page cannot prove rejection even after the ordinary
    # confirmation timeout. Only a later complete read can settle this write.
    monkeypatch.setattr(controller.ctx, 'now_fn', lambda: 1295.0)
    with pytest.raises(LibrarySnapshotError, match='continuation.envelope'):
        play(restored, 'v3')
    assert restored.session.pending['video_id'] == 'v4'
    assert not restored.session.halted and not restored.session.rejected
    remote.after_add = lambda: None
    play(restored, 'v3')
    assert restored.session.pending is None and restored.status()['ahead'] == 1
    assert remote.ids == ['v1', 'v2', 'v3', 'v4'] and remote.adds == [['v4']]
    assert len(frames) == 1, 'Readback recovery must not repeat navigation'


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
