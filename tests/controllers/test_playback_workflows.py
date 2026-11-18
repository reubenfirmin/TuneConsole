"""Playback admission, projection and optional acquisition need no database or ASGI app."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock
import json
from threading import Event

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playback_observations import PlaybackObservations, admit, player_projection
from yt_playlist.controllers.playback_commands import PlaybackCommands
from yt_playlist.controllers.tv_commands import playback_message
from yt_playlist.controllers.tv_artwork import TVArtwork, artwork_value
from yt_playlist.controllers.audio_capture import AudioCapture
from yt_playlist.controllers.radio_commands import RadioCommands
from yt_playlist.rec import audio_vibe_prediction

pytestmark = pytest.mark.usefixtures('no_database')


def test_admission_is_exact_account_scoped_and_detaches_mutable_frames():
    accounts = [NS(id=1, brand_account_id='brand'), NS(id=2, brand_account_id='other')]
    body = dict(brandId='brand', brandKnown=True, title='Title', videoId='upload')
    observation = admit('live', accounts, body, 20)
    body['title'] = 'Later'
    assert observation.account_id == 1 and observation.body['title'] == 'Title'
    assert admit('live', accounts + [NS(id=3, brand_account_id='brand')], body, 20) is None
    assert admit('live', accounts, {**body, 'muted': True}, 20) is None
    assert admit('player', accounts, dict(kind='rate', brandId='brand', brandKnown=False), 20) is None


def test_projection_separates_confirmed_ratings_and_exit_evidence():
    body = dict(kind='rate', url='https://music.youtube.com/youtubei/v1/like/like',
        body=json.dumps({'target': {'videoId': 'exact-upload'}}), phase='settled', success=True, httpStatus=200)
    assert player_projection(body)['rating'] == 'LIKE'
    assert player_projection({**body, 'success': False})['rating'] is None
    assert player_projection(dict(kind='track_exit', videoId='exact-upload', position=11))['exit']


def test_admitted_observation_uses_durable_capture_before_projection():
    ports = [Mock() for _ in range(5)]
    accounts, tracks, history, settings, events = ports
    accounts.get_accounts.return_value = [NS(id=3, brand_account_id='')]
    capture, confirmed, refresh, exit = [Mock() for _ in range(4)]
    deferred = Mock(return_value=True)
    command = PlaybackObservations(*ports, deferred=deferred, capture=capture,
        album_context=Mock(), record_playback=Mock(), capture_start=Mock(), observe_exit=exit,
        rating_observations=Mock(), confirmed_rating=confirmed, reconcile_ratings=Mock(), refresh_ratings=refresh)
    frame = dict(kind='ended', brandId='', videoId='upload', position=100, duration=100)
    assert command.observe('player', frame, 20)
    capture.assert_called_once_with('player', 3, frame, 20)
    events.record_player_event.assert_not_called()
    command.project(admit('player', accounts.get_accounts(), frame, 20), capture_id='durable')
    assert events.record_player_event.call_args.kwargs == {'capture_id': 'durable'}
    assert exit.call_args.args[0:4] == (3, None, 'upload', 'ended')
    confirmed.assert_not_called()


def test_status_joins_captured_player_state_with_explicit_library_ports():
    player = NS(now_playing={'video_id': 'upload', 'playlist_ytm_id': 'remote'}, now_playing_seen_at=98,
                connected=True, resumable_player=None)
    tracks = Mock()
    tracks.duration_for_video.return_value = 210
    tracks.thumbnail_for_video.return_value = 'art'
    collection = Mock()
    collection.playback_source.return_value = dict(kind='playlist', id=4, title='Saved')
    command = PlaybackCommands(player, Mock(), tracks, Mock(), Mock(), collection,
        radio=None, work=Mock(), now=lambda: 20, ops=Mock(), rating=lambda video: {'status': 'LIKE'},
        identity=Mock(), duration=Mock(), record_next=Mock(), paused=lambda: False,
        measure=Mock(), logger=Mock(), monotonic=lambda: 100)
    state = command.status()
    assert state['now_playing']['duration'] == 210 and state['now_playing']['source']['url'] == '/playlist/4'
    command.monotonic = lambda: 110
    assert command.status()['now_playing'] is None


def test_tv_event_interpretation_and_artwork_acquisition_never_get_a_player_port():
    pl = NS(account_id=7, provider_playlist_id='exact-playlist')
    item = dict(video_id='exact-upload', title='Video', artist='Artist', duration=120)
    frame = playback_message(pl, [item], [NS(id=7, brand_account_id='brand')],
        dict(kind='tick', video_id='exact-upload', position='12'))
    assert frame['brandId'] == 'brand' and frame['videoId'] == 'exact-upload'
    with pytest.raises(ValueError):
        playback_message(pl, [item], [NS(id=7, brand_account_id='brand')],
                         dict(kind='tick', video_id='exact-upload', position='nan'))
    proposals, observations = Mock(), Mock()
    proposals.get_proposals.return_value = {}
    observations.publish_artwork.return_value = True
    command = TVArtwork(proposals, observations, images=lambda name: (['local'], {}),
        search=lambda name: ([], True), vendor=lambda name, now: (['remote'], False, 500))
    assert command.prepare(['Artist'], 20) is False
    value = observations.publish_artwork.call_args.args[1]
    assert value == artwork_value(['local', 'remote'], 20, True, 500)
    assert observations.publish_artwork.call_args.kwargs == {'complete': False}


def test_audio_feedback_reuses_frozen_prediction_and_rejects_bad_samples_before_write():
    captures, predict = Mock(), Mock(return_value={'model': 'captured'})
    captures.playback.return_value = {'video_id': 'upload'}
    captures.samples.return_value = [{'id': 'one', 'video_id': 'upload'}]
    captures.feedback.return_value = {'sample_ids': ['one'], 'prediction': {'model': 'frozen'}}
    command = AudioCapture(captures, publication=nullcontext(), enabled=lambda: False, now=lambda: 20, predict=predict)
    command.feedback('run', ['one'], [], audio_vibe_prediction.TAXONOMY_VERSION)
    assert captures.save_feedback.call_args.args[4] == {'model': 'frozen'}
    predict.assert_not_called()
    captures.save_feedback.reset_mock()
    with pytest.raises(ValueError):
        command.feedback('run', ['missing'], [], audio_vibe_prediction.TAXONOMY_VERSION)
    captures.save_feedback.assert_not_called()


def test_radio_capture_pins_policy_and_prepared_generation_before_decoding(monkeypatch):
    from yt_playlist.rec import radio_candidates
    reader = Mock()
    reader.settings.get_setting.side_effect = lambda name: {'rec_content_generation': 'generation',
                                                            'rec_content_model': 'model'}[name]
    reader.modes.list_modes.return_value = [{'mode_id': 3, 'space': 'space'}]
    active = []
    @contextmanager
    def read_view(**kwargs):
        active.append(reader)
        try:
            yield reader
        finally:
            active.clear()
    store = NS(_read_only=False, _lock=NS(held_by_current_thread=False), read_view=read_view)
    monkeypatch.setattr(radio_candidates.vectors, 'content_space_id', lambda *args, **kwargs: 'space')
    def capture(value):
        assert active == [value]
        return {'blocked'}, {'muted'}
    def capture_prepared(value, modes, space, generation, blocked, muted, comfort):
        assert active == [value]
        assert generation == 'generation' and blocked == {'blocked'} and muted == {'muted'}
        return 'encoded'
    def decode(value):
        assert not active and value == 'encoded'
        return {'3': ['detached']}
    prepared = NS(capture_inputs=capture_prepared, decode_inputs=decode)
    result = radio_candidates._prepared_inputs(store, prepared=prepared, capture=capture)
    assert result[-1] == {'3': ['detached']}


def test_detached_radio_ranking_honors_feedback_and_eligibility():
    from yt_playlist.rec import radio_ranking
    tuning = dict(theme=.5, comfort=.5, energy=.5)
    track = dict(plays=0, liked=False)
    facts = dict(prior=.5, recent=[.9, .8], target=.9, tuning=tuning, breadth=.5, genre_bonus=0)
    liked = radio_ranking.candidate_score(track, feedback=[(1, .9)], **facts)
    disliked = radio_ranking.candidate_score(track, feedback=[(-1, .9)], **facts)
    assert liked > disliked
    rows = [dict(key='blocked'), dict(key='allowed')]
    decisions = {key: dict(allowed=key == 'allowed', contrast=0, fits=True, artist_gap=7)
                 for key in ('blocked', 'allowed')}
    assert radio_ranking.choice_tier(rows, decisions, energy=.5, breadth=.5) == rows[1:]


def test_radio_catalog_continues_past_rejected_pages_without_replacing_uploads():
    from yt_playlist.controllers.radio_inputs import RadioCatalog
    from yt_playlist.rec.radio_catalog import eligible_page
    rows = [dict(id=i, key=key, video_id=video) for i, key, video in
            [(1, 'blocked', 'blocked'), (2, 'blocked', 'other-upload'),
             (3, 'one', 'exact-one'), (4, 'two', 'exact-two')]]
    repo = Mock(spec_set=['fallback_tracks'])
    repo.fallback_tracks.side_effect = lambda size, after: [r for r in rows if r['id'] > after][:size]
    allowed = lambda track: track['key'] != 'blocked'
    selected = RadioCatalog(repo).fallback(allowed, limit=2)
    assert [t['video_id'] for t in selected] == ['exact-one', 'exact-two']
    assert [call.kwargs['after'] for call in repo.fallback_tracks.call_args_list] == [0, 2]
    assert eligible_page(rows, (), (), allowed, 0) == []


def test_tv_preparation_preserves_output_when_source_changes_and_retains_refresh_request():
    from yt_playlist.controllers.tv_preparation import TVPoolPreparation
    state = dict(pool={'at': 1, 'items': ['old']}, request=0, current=True)
    publication, recorded = Mock(), Mock()
    guard = NS(store=object(), is_current=lambda: state['current'])
    def publish(write):
        if not state['current']:
            return False
        write()
        return True
    guard.publish = publish
    def prepare(reader, client, now, previous, publish):
        state.update(current=False, request=1)
        return not publish({'at': now, 'items': ['stale']})
    command = TVPoolPreparation(snapshot=lambda: nullcontext(guard), cached=lambda: state['pool'],
        requested=lambda: state['request'], publish=publication, record=recorded, prepare=prepare,
        expiry=lambda *args: None, tuning=lambda *args: 0, max_age=lambda: 10, retry_seconds=lambda: 2)
    assert command.step(object(), 2)
    publication.assert_not_called()
    assert state['pool']['items'] == ['old'] and command.due(2)
    state['current'] = True
    def prepare_current(reader, client, now, previous, publish):
        state['request'] += 1
        publish({'at': now, 'items': ['current']})
        return False
    publication.side_effect = lambda payload, now: state.update(pool=payload)
    command.prepare = prepare_current
    assert command.step(object(), 3), 'A request received during preparation must remain due'
    assert state['pool']['items'] == ['current'] and command.due(3)


def test_radio_admits_intent_before_worker_and_cancellation_prevents_start():
    radio = Mock(spec_set=['launch_request', 'bootstrap', 'start'])
    cancelled = Event()
    radio.launch_request.side_effect = lambda launch_id: nullcontext(cancelled)
    radio.bootstrap.return_value = dict(state={'active': False}, seedMenu='menu',
        seeds=[{'video_id': 'exact-upload'}])
    player = Mock(spec_set=['begin_playback_intent'])
    player.begin_playback_intent.return_value = 17
    settings = Mock(spec_set=['get_setting'])
    settings.get_setting.return_value = 'radio'
    require = Mock()
    command = RadioCommands(radio, player, settings, completed=lambda: False,
        require=require, logger=Mock())
    data = dict(video_id='exact-upload', menu_id='menu', launch_id='request')
    with command.admit_start(data) as (intent, token):
        assert intent == 17 and token is cancelled
        player.begin_playback_intent.assert_called_once()
        radio.start.assert_not_called()
        command.start(data, intent, token)
    require.assert_not_called()
    radio.start.assert_called_once_with('exact-upload', None, 'menu',
        playlist_id=None, intent=17, cancelled=cancelled)
    cancelled.set()
    radio.start.reset_mock()
    player.begin_playback_intent.reset_mock()
    with command.admit_start(data) as (intent, token):
        assert intent is None
        assert not command.start(data, intent, token)['ok']
    player.begin_playback_intent.assert_not_called()
    radio.start.assert_not_called()


def test_loaded_audio_model_merge_preserves_other_evidence_without_file_access(monkeypatch):
    from tests.test_audio_vibe_cruise import samples
    from yt_playlist.core.audio_embedding import signature
    from yt_playlist.core.audio_model_files import AudioModelFiles
    from yt_playlist.rec.audio_vibe_runtime import apply_loaded
    def forbidden(*args, **kwargs):
        pytest.fail('Scoring supplied model values must not read files or configuration')
    for operation in ('load', 'info', 'configured'):
        monkeypatch.setattr(AudioModelFiles, operation, forbidden)
    rows = samples()
    model = {'header': {'embedding_signature': signature(rows[0])}, 'sha256': 'a' * 64}
    baseline = dict(ranking=[dict(vibe='cruise', score=.2), dict(vibe='release', score=.7)],
        suitability={'release': .9}, individual_models={'release': {'score': .9}})
    before = deepcopy((rows, model, baseline))
    scorer = Mock(return_value={'score': .8, 'source': 'supplied-coefficients'})
    active = apply_loaded(rows, baseline, selected_mode='active', model=model, scorer=scorer, vibe='cruise')
    assert active['suitability'] == {'cruise': .8, 'release': .9}
    assert active['individual_models']['release'] == {'score': .9}
    shadow = apply_loaded(rows, baseline, selected_mode='shadow', model=model, scorer=scorer, vibe='cruise')
    assert shadow['suitability'] == {'release': .9}
    assert shadow['ranking'] == baseline['ranking']
    assert (rows, model, baseline) == before
