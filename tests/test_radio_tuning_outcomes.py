"""Explicit stream tuning owns its values through admission and publication."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import poc, play
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import _file_consumer_store, bounded_consumer_schema, native_sql_calls
from yt_playlist.core import activity
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import account_growth
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.web.routes import radio_session as routes


@pytest.fixture
def store(tmp_path, native_sql_calls):
    # Tuning consumes the complete 32-track catalog. Its concurrent requests,
    # actual session checkpoints and snapshot guards remain private and real.
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        with _file_consumer_store(tmp_path, schema) as database:
            from yt_playlist.util.matching import identity_key

            pool = [{'key': identity_key(f'Song {i}', f'Artist {i}'),
                     'video_id': f'v{i}', 'title': f'Song {i}', 'artist': f'Artist {i}'}
                    for i in range(1, 33)]
            seed_publication_subjects(database, pool)
            database._fixture_radio_pool = pool
            yield database


@pytest.mark.parametrize('access', [False, True])
def test_energy_admission_has_an_explicit_receipt_without_the_full_journey(poc, monkeypatch, caplog, access):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    monkeypatch.setattr(account_growth, 'retained_access', lambda *args: access)
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *args: False)
    monkeypatch.setattr(account_growth, 'view', lambda *args, **kwargs: pytest.fail('Full journey read'))
    app = FastAPI()
    app.include_router(routes.build(c.ctx))
    values = {'comfort': .1, 'theme': .9, 'energy': .2}
    with caplog.at_level('INFO'), TestClient(app) as client:
        result = client.post('/radio/tune', json=values).json()
        assert result['ok'] is access
        if access:
            assert result['tuning'] == values
            assert client.get('/radio/status').json()['tuning'] == values
            assert c.persistence.load()['tuning'] == values
            assert 'Radio tuning admitted' in caplog.text
        else:
            assert result['tune_reason'] == 'energy_locked'
            assert c.status()['tuning'] == {'comfort': .5, 'theme': .5, 'energy': .5}
            assert c.session.requested_direction is None
            assert 'reason=energy_locked' in caplog.text
    assert (remote.ids, remote.adds, remote.removes, frames) == before


def test_a_newer_tuning_request_supersedes_a_late_plan(poc, monkeypatch):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    original = c.session.directions
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    entered, release = threading.Event(), threading.Event()
    admitted, admissions = threading.Event(), []
    reserve = c.reserve_tuning

    def reserved():
        serial = reserve()
        admissions.append(serial)
        if len(admissions) == 2:
            admitted.set()
        return serial

    monkeypatch.setattr(c, 'reserve_tuning', reserved)
    update = RadioDirections.update

    def held(*args, **kwargs):
        result = update(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(RadioDirections, 'update', held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(c.tune, {'comfort': .1, 'theme': .5, 'energy': .5})
        try:
            assert entered.wait(3)
            second = pool.submit(c.tune, {'comfort': .9, 'theme': .5, 'energy': .5})
            assert admitted.wait(3)
            assert c.status()['menu_id'] == original.menu_id
        finally:
            release.set()
        assert not first.result(timeout=5)['ok']
        assert second.result(timeout=5)['ok']
    assert c.session.directions is not original
    assert c.persistence.load()['tuning']['comfort'] == .9
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('completed', [False, True])
def test_selection_during_tuning_admission_supersedes_that_apply(poc, completed):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    serial = c.reserve_tuning()
    state = c.status()
    branch = state['directions'][2]
    assert c.select(branch['id'], state['menu_id'])['ok']
    if completed:
        # The observer can finish the newer choice while Apply is still in
        # admission. Returning to an empty journal must not revive older work.
        c.advance(c.session)
        c.advance(c.session)
        assert c.session.requested_direction is None
        assert c.session.replacement is None
        assert not c.status()['updating']
        assert c.session.directions.active == branch['slot']
    request, original = c.session.requested_direction, c.session.directions
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    assert not c.tune({'comfort': .1, 'theme': .9, 'energy': .2}, serial=serial)['ok']
    assert c.session.requested_direction is request and c.session.directions is original
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('selection', ['unchanged', 'stale'])
def test_selection_without_a_new_choice_keeps_tuning_admission(poc, selection):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    serial = c.reserve_tuning()
    state = c.status()
    branch = next(branch for branch in state['directions'] if branch['active'])
    result = c.select(branch['id'], state['menu_id'] if selection == 'unchanged' else 'stale-menu')
    assert result['ok'] is (selection == 'unchanged')
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    values = {'comfort': .1, 'theme': .9, 'energy': .2}
    result = c.tune(values, serial=serial)
    assert result['ok'] and result['tuning'] == values
    assert c.persistence.load()['tuning'] == values
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('failure', ['cancelled', 'rebase'])
def test_unfinished_tuning_keeps_the_last_published_candidates_and_menu(poc, monkeypatch, failure):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    source, original = c.session.candidates, c.session.directions
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    update, calls = RadioDirections.update, []

    def unfinished(*args, **kwargs):
        update(*args, **kwargs)
        calls.append(1)
        if failure == 'cancelled':
            raise WorkClosing('controlled cancellation')
        if len(calls) <= 4:
            with c.lock:
                c.session.metadata_revision += 1

    monkeypatch.setattr(RadioDirections, 'update', unfinished)
    result = c.tune({'comfort': .1, 'theme': .9, 'energy': .2})
    assert result['ok'] is (failure == 'rebase')
    assert len(calls) == (1 if failure == 'cancelled' else 5)
    assert (c.session.candidates is source) is (failure == 'cancelled')
    assert (c.session.directions is original) is (failure == 'cancelled')
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('save_fails', [False, True])
def test_completed_tuning_is_durable_before_confirmation_and_same_values_repair_a_failed_save(poc, monkeypatch, save_fails):
    c, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    values = {'comfort': .1, 'theme': .9, 'energy': .5}
    tune, save = c.listening.tune, c.persistence.checkpoints.save
    token = []

    def finish_request():
        while token:
            activity._DEADLINE.reset(token.pop())

    def published(*args, **kwargs):
        result = tune(*args, **kwargs)
        # Reproduce the reviewer's post-publication deadline boundary without
        # making this test wait. No disposable deadline may skip the save.
        token.append(activity._DEADLINE.set(time.monotonic() - 1))
        return result

    monkeypatch.setattr(c.listening, 'tune', published)
    try:
        if save_fails:
            def failed(*args, **kwargs):
                raise RuntimeError('checkpoint write failed')
            monkeypatch.setattr(c.persistence.checkpoints, 'save', failed)
            with pytest.raises(RuntimeError, match='checkpoint write failed'):
                c.tune(values)
            finish_request()
            assert c.status()['tuning'] == values
            assert not c.status()['tuning_confirmed']
            assert c.persistence.load()['tuning'] != values
            monkeypatch.setattr(c.persistence.checkpoints, 'save', save)
        result = c.tune(values)
        finish_request()
        assert result['ok'] and result['tuning_confirmed']
        assert c.persistence.load()['tuning'] == values
        c.suspend()
        restored = type(c)(c.ctx)
        try:
            assert restored.join()['tuning'] == values
        finally:
            restored.suspend()
    finally:
        finish_request()


def test_new_session_during_tuning_admission_supersedes_that_apply(poc):
    c, remote, frames, *_ = poc
    assert c.session is None
    serial = c.reserve_tuning()
    # An explicit Start differs from resuming the checkpoint that Apply was
    # intended for. Its session must not inherit the earlier pending request.
    assert c.start()['ok']
    play(c, 'v2')
    session = c.session
    source, original = session.candidates, session.directions
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    result = c.tune({'comfort': .1, 'theme': .9, 'energy': .2}, serial=serial)
    assert not result['ok'] and result['tune_reason'] == 'superseded'
    assert session.candidates is source and session.directions is original
    assert session.requested_direction is None
    assert c.status()['tuning'] == {'comfort': .5, 'theme': .5, 'energy': .5}
    assert (remote.ids, remote.adds, remote.removes, frames) == before


def test_unloaded_saved_session_can_restore_for_its_reserved_tuning(poc):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    c.suspend()
    c._persist(c.session)
    restored = type(c)(c.ctx)
    c.ctx.radio = restored
    try:
        assert restored.session is None
        serial = restored.reserve_tuning()
        before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
        values = {'comfort': .1, 'theme': .9, 'energy': .2}
        result = restored.tune(values, serial=serial)
        assert result['ok'] and result['tuning'] == values
        assert restored.persistence.load()['tuning'] == values
        assert restored.session.playlist == c.session.playlist
        assert (remote.ids, remote.adds, remote.removes, frames) == before
    finally:
        restored.suspend()


def test_saved_checkpoint_distinguishes_missing_playback_presence_from_stop(poc, monkeypatch):
    c, remote, frames, *_ = poc
    assert c.start()['ok']
    play(c, 'v2')
    values = {'comfort': .1, 'theme': .9, 'energy': .2}
    assert c.tune(values)['ok']
    c.suspend()
    c._persist(c.session)
    restored = type(c)(c.ctx)
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    checkpoint = restored.persistence.load()
    bridge = c.ctx.bridge
    for presence in ('disconnected', 'stale', 'paused'):
        with monkeypatch.context() as patch:
            if presence == 'disconnected':
                patch.setattr(bridge, 'connected', False)
            elif presence == 'stale':
                patch.setattr(bridge, 'now_playing_seen_at', time.monotonic() - 9)
            else:
                patch.setattr(bridge, 'now_playing', {**bridge.now_playing, 'paused': True})
            state = restored.state()
            assert not state['active'] and not state['stopped']
            assert state['playlist_id'] == c.session.playlist and state['tuning'] == values
            assert restored.session is None
            assert restored.persistence.load() == checkpoint
    assert restored.state()['active'] and not restored.state()['stopped']
    stopped = c.stop()
    assert stopped['ok'] and stopped['stopped'] and not stopped['active']
    state = restored.state()
    assert state['stopped'] and not state['active']
    assert state['playlist_id'] == c.session.playlist and state['tuning'] == values
    assert (remote.ids, remote.adds, remote.removes, frames) == before
