"""Incomplete provider pages defer confirmation without replaying a saved edit."""
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ytmusicapi.navigation import PLAY_BUTTON

from tests.core.test_playlist_page_accounting import first, item, token
from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.web.radio_edits import AppendOperation, RadioEdits
from yt_playlist.web.radio_mutations import PlaylistTarget, RadioMutations
from yt_playlist.web.radio_playlist_io import PlaylistEdit, PlaylistPending, RadioPlaylistIO
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail, TailView


METADATA_ONLY = {'responseContext': {}, 'trackingParams': 'PRIVATE_TRACKING'}


def _playlist_page(videos, *, continuation=False):
    rows = []
    for video in videos:
        row = item(video)
        button = row['musicResponsiveListItemRenderer']
        for field in PLAY_BUTTON:
            button = button.setdefault(field, {})
        button['playNavigationEndpoint'] = {'watchEndpoint': {'videoId': video}}
        rows.append(row)
    if continuation:
        rows.append(token('PRIVATE_CONTINUATION'))
    return first(rows, count=len(videos))


@pytest.mark.parametrize('expected', [None, ['a', 'b']])
def test_metadata_only_continuation_is_pending_even_when_initial_rows_match(expected):
    client, bridge = _production_client([
        _playlist_page(['a', 'b'], continuation=True), METADATA_ONLY])
    io = RadioPlaylistIO(None)
    with pytest.raises(PlaylistPending) as caught:
        io.read(client, 'PL', expected=expected)
    cause = caught.value.__cause__
    assert isinstance(cause, LibrarySnapshotError)
    assert cause.page == 2 and 'metadata_only' in str(cause)
    assert 'PRIVATE_' not in str(cause)
    assert len(bridge.calls) == 2  # No nested retry loop or partial return.


@pytest.mark.parametrize('replacement', [False, True], ids=['append', 'tail'])
def test_incomplete_confirmation_retains_write_until_complete_readback(replacement):
    before = {'video_id': 'a', 'duration': 180}
    added = {'video_id': 'b', 'duration': 180}
    edit = PlaylistEdit('add', ('a', 'b'), track=added)
    operation = RadioPlaylistTail([before], [added], 0) if replacement else AppendOperation(added)
    edits = RadioEdits(active=operation)
    clock = [10.]
    incomplete = [True]
    remote_ids, reads, writes, journals, published = ['a'], [], [], [], []

    def read(playlist, limit=None):
        # Reproduce the production reader's actual page-2 exception after the
        # provider has already accepted the mutation, not just a parser stub.
        broken = len(remote_ids) == 2 and incomplete[0]
        responses = [_playlist_page(remote_ids, continuation=broken)]
        if broken:
            responses.append(METADATA_ONLY)
        client, bridge = _production_client(responses)
        try:
            return client.get_playlist(playlist, limit=limit)
        finally:
            reads.append(len(bridge.calls))

    def add(playlist, videos):
        assert journals[-1].write is not None
        writes.append(videos)
        remote_ids.extend(videos)
        return {'status': 'STATUS_SUCCEEDED'}

    persistence = SimpleNamespace(record_edit=lambda episode, op: journals.append(deepcopy(op)))
    io = RadioPlaylistIO(SimpleNamespace(publish_confirmed_tracks=lambda *a, **kw: published.append(a)))
    owner = RadioMutations(io, persistence, None, lambda: clock[0])
    target = PlaylistTarget(SimpleNamespace(get_playlist=read, add_playlist_items=add),
        'PL', 1, 'Radio', 'episode', threading.Event())
    view = TailView((before,), 0, 0, frozenset(), {'a': before, 'b': added})

    with pytest.raises(PlaylistPending):
        if replacement:
            owner.edit_tail(target, edits, view, edit, (before,))
        else:
            owner.append(target, edits, view)
    pending = operation.write
    assert pending is not None and journals[-1].write is not None
    assert pending.receipt == {'status': 'STATUS_SUCCEEDED'}
    assert writes == [['b']] and remote_ids == ['a', 'b'] and not published

    # An incomplete read cannot prove absence, even beyond the normal timeout.
    for now in (45., 90.):
        clock[0] = now
        previous_reads = len(reads)
        with pytest.raises(PlaylistPending):
            if replacement:
                owner.read_tail(target, operation, view)
            else:
                owner.append(target, edits, view)
        assert len(reads) == previous_reads + 1 and reads[-1] == 2
        assert operation.write is pending and not published and writes == [['b']]

    incomplete[0] = False
    result = (owner.read_tail(target, operation, view) if replacement
              else owner.append(target, edits, view))
    assert result.status == 'confirmed'
    assert [track['video_id'] for track in result.tracks] == remote_ids
    assert len(published) == 1 and writes == [['b']]


def test_unexpected_provider_programming_errors_are_not_hidden_as_pending():
    client = SimpleNamespace(get_playlist=Mock(side_effect=ValueError('unexpected bug')))
    with pytest.raises(ValueError, match='unexpected bug'):
        RadioPlaylistIO(None).read(client, 'PL')
