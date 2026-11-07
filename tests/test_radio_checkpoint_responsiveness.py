"""A dirty Radio checkpoint must not block live projection on a busy writer."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import poc, play
from yt_playlist.core import radio_storage
from yt_playlist.web.radio_edits import AppendOperation
from yt_playlist.web.radio_persistence import encode, restore
from yt_playlist.web.radio_playlist_io import PlaylistEdit
from yt_playlist.web.routes import radio_session
from yt_playlist.web.routes.bridge import build as bridge_routes


@pytest.fixture
def store(file_backed_store, radio_catalog_template):
    # Production can use independent WAL readers for player metadata. An
    # in-memory Store would introduce unrelated serialization in bridge status.
    file_backed_store._fixture_radio_pool = radio_catalog_template.restore(file_backed_store)
    return file_backed_store


def test_checkpoint_waiting_for_writer_leaves_live_status_available(poc, monkeypatch):
    controller, remote, frames, *_ = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    controller.ctx.bridge.resumable_player = None
    session = controller.session
    # A fresh Now sample changes a real encoded checkpoint field. Skipping an
    # identical save must not conceal the required dirty-checkpoint behavior.
    session.candidates.now_sampled_at += 1
    entered = threading.Event()
    save = controller.persistence.save

    def saving(current):
        entered.set()
        return save(current)

    monkeypatch.setattr(controller.persistence, 'save', saving)
    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))
    app.include_router(bridge_routes(controller.ctx))
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    checkpoint, polls = None, []
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as pool:
        try:
            # Hold the actual writer lock, rather than replacing persistence
            # with a fake success. Live projection needs no writer admission.
            with controller.ctx.store._lock:
                checkpoint = pool.submit(controller._persist, session)
                assert entered.wait(3), 'Checkpoint save was not attempted'
                polls = [pool.submit(client.get, path)
                         for path in ('/radio/status', '/bridge/status')]
                radio, bridge = [poll.result(timeout=.5) for poll in polls]
                assert radio.status_code == bridge.status_code == 200
                assert radio.json()['active']
                assert radio.json()['playlist_id'] == 'PLSESSION'
                assert bridge.json()['radio']
        finally:
            # Leave the writer context before joining any blocked work. Failure
            # must release both HTTP requests and the real checkpoint save.
            if checkpoint is not None:
                checkpoint.result(timeout=5)
            for poll in polls:
                poll.result(timeout=5)
    assert controller.persistence.load()['candidate_context']['now_sampled_at'] == session.candidates.now_sampled_at
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('newer', ['stop', 'song', 'journal', 'session'])
def test_waiting_checkpoint_cannot_replace_a_later_saved_owner(poc, monkeypatch, newer):
    controller, remote, frames, _, pool = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    original = controller.session
    original.candidates.now_sampled_at += 1
    entered, release = threading.Event(), threading.Event()
    owner = [None]
    save = controller.persistence.save
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))

    def held(current):
        if threading.get_ident() == owner[0]:
            entered.set()
            assert release.wait(5), 'The test did not release the older save'
        return save(current)

    def older_save():
        owner[0] = threading.get_ident()
        controller._persist(original)

    def supersede():
        if newer == 'stop':
            assert controller.stop()['ok']
        elif newer == 'song':
            state = controller.status()
            branch = state['directions'][1]
            assert controller.select(branch['id'], state['menu_id'], branch['tracks'][2]['video_id'])['ok']
        elif newer == 'journal':
            # Exercise the real durable-before-transport protocol. The late
            # save must retain its sent flag/receipt and never resend the add.
            track = pool[-1]
            operation = AppendOperation(track)
            with controller.lock:
                original.edits.begin(operation)
                original.settle()
            controller._persist(original)
            edit = PlaylistEdit('add', tuple(t['video_id'] for t in original.tracks) + (track['video_id'],), track=track)
            assert controller.mutations._send(controller._target(original), original.edits, operation, edit)
        else:
            # Controlled session replacement isolates checkpoint ownership
            # from provider creation; restore itself never writes or navigates.
            replacement = restore(json.loads(encode(original)), remote, original.identity, original.candidates)
            replacement.episode_id = 'newer-checkpoint-owner'
            with controller.lock:
                controller.session = replacement
            controller._persist(replacement)
        return radio_storage.load_published(controller.ctx.store.conn)

    monkeypatch.setattr(controller.persistence, 'save', held)
    with ThreadPoolExecutor(max_workers=2) as workers:
        older = workers.submit(older_save)
        later = None
        try:
            assert entered.wait(2), 'Older checkpoint did not reach the controlled save boundary'
            later = workers.submit(supersede)
            expected = later.result(timeout=1)
            assert not older.done(), 'The interleaving must keep the older save pending'
        finally:
            release.set()
            older.result(timeout=5)
            if later is not None:
                later.result(timeout=5)
    assert radio_storage.load_published(controller.ctx.store.conn) == expected
    state = controller.persistence.load()
    if newer == 'stop':
        assert state['stopped'] and controller.session.stop.is_set()
    elif newer == 'song':
        assert state['requested_direction']['slot'] == 1
        assert 'play' not in state['requested_direction'], 'A restored checkpoint must not replay a song click'
        assert controller.session.requested_direction['play']['intent'] is not None
    elif newer == 'journal':
        assert state['pending_sent'] and state['pending_result'] == {'status': 'STATUS_SUCCEEDED'}
        assert remote.adds == [[pool[-1]['video_id']]]
        assert remote.removes == before[2] and frames == before[3]
    else:
        assert state['episode_id'] == 'newer-checkpoint-owner'
    if newer != 'journal':
        assert (remote.ids, remote.adds, remote.removes, frames) == before


def test_stop_while_checkpoint_waits_for_writer_keeps_live_status_available(poc, monkeypatch):
    controller, remote, frames, *_ = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    controller.ctx.bridge.resumable_player = None
    session = controller.session
    session.candidates.now_sampled_at += 1
    saving, stopped = threading.Event(), threading.Event()
    write, request_stop = controller.persistence._write, session.request_stop

    def waiting(encoded):
        saving.set()
        return write(encoded)

    def stop_requested():
        request_stop()
        stopped.set()

    monkeypatch.setattr(controller.persistence, '_write', waiting)
    monkeypatch.setattr(session, 'request_stop', stop_requested)
    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))
    app.include_router(bridge_routes(controller.ctx))
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    checkpoint, stopping, polls = None, None, []
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as workers:
        try:
            with controller.ctx.store._lock:
                checkpoint = workers.submit(controller._persist, session)
                assert saving.wait(2), 'Dirty checkpoint did not reach writer admission'
                stopping = workers.submit(controller.stop)
                assert stopped.wait(.5), 'Stop could not update live state while the writer was held'
                polls = [workers.submit(client.get, path) for path in ('/radio/status', '/bridge/status')]
                radio, bridge = [poll.result(timeout=.5) for poll in polls]
                assert radio.status_code == bridge.status_code == 200
                assert not radio.json()['active'] and radio.json()['stopped']
                assert not bridge.json()['radio']
                assert not checkpoint.done() and not stopping.done(), 'Durability must await the real writer'
        finally:
            # Release the actual writer before joining every dependent task,
            # including when a responsiveness assertion detects the old defect.
            for future in [checkpoint, stopping, *polls]:
                if future is not None:
                    future.result(timeout=5)
    saved = controller.persistence.load()
    assert saved['stopped'] and saved['candidate_context']['now_sampled_at'] == session.candidates.now_sampled_at
    assert (remote.ids, remote.adds, remote.removes, frames) == before
