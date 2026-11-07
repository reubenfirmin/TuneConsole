"""Readiness decisions compose domain facts without storage formats or SQL."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from yt_playlist.rec import artist_start, onboarding_outputs


def test_generated_starter_requires_a_live_start_while_failed_album_import_can_retry():
    reader = SimpleNamespace(
        _read_only=True,
        readiness=Mock(playable_owned=Mock(return_value=False),
                       live_starter_played=Mock(return_value=False)),
        collection=Mock(get_saved_albums=Mock(return_value=[])),
        discovery=Mock(starter_artists=Mock(return_value=[{}, {}, {}]),
                       starter_tracks=Mock(return_value=[{'video_id': 'starter'}])),
        settings=Mock(get_setting=Mock(return_value=None)),
        accounts=Mock(get_accounts=Mock(return_value=[SimpleNamespace(id=1)])),
        sync=Mock(state=Mock(return_value={'accounts': {'1': {'resources': {
            'playlist_listing': {'status': 'complete'}, 'saved_albums': {'status': 'failed'}}}}})))
    assert artist_start.required(reader)
    reader.readiness.playable_owned.assert_called_once_with(exclude_generated=True)
    reader.readiness.live_starter_played.assert_called_once_with(['starter'])
    reader.readiness.live_starter_played.return_value = True
    assert not artist_start.required(reader)
    reader.sync.state.assert_called_once_with()


def test_starter_album_prefers_a_saved_release_then_uses_detached_familiar_albums():
    single = {'browse': 'MPREsingle', 'title': 'Single', 'type': 'Single'}
    album = {'browse': 'MPREalbum', 'title': 'Album', 'type': 'Album'}
    reader = SimpleNamespace(collection=Mock(get_saved_albums=Mock(return_value=[single, album])),
        readiness=Mock(familiar_albums=Mock(return_value=[{'browse': 'bad', 'title': 'Unusable'}, album])))
    store = SimpleNamespace(read_view=lambda: nullcontext(reader))
    assert onboarding_outputs.starter_album(store) == {**album, 'saved': True, 'upload': False}
    reader.readiness.familiar_albums.assert_not_called()
    reader.collection.get_saved_albums.return_value = []
    assert onboarding_outputs.starter_album(store) == {**album, 'saved': False, 'upload': False}
