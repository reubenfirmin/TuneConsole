"""A running Radio controller retains its journal across the collection cutover."""
import json
from types import SimpleNamespace

import pytest

from yt_playlist.core import catalog, entity_names, radio_storage
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.store import Store
from yt_playlist.web.radio_edits import AppendOperation
from yt_playlist.web.radio_persistence import RadioPersistence, encode
from yt_playlist.web.radio_playlist_io import PendingWrite, PlaylistEdit
from yt_playlist.web.radio_state import Phase, Session


@pytest.fixture
def predecessor(tmp_path):
    store = Store(tmp_path / 'radio.db')
    migrate(store.conn, registry=registry.MIGRATIONS[:93])
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    assert [(row[0], row[1]) for row in history] == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS[:93]]
    store.init_schema(defer_metadata=True)
    assert [tuple(row) for row in store.conn.execute(
        'SELECT * FROM schema_migrations ORDER BY version')] == history
    account = store.accounts.upsert_account('Radio', 'radio.json', None, True)
    local_id = store.playlists.upsert_playlist(account, 'RADIO', 'Radio', 1, 'fixture', 1)
    tracks = [
        {'video_id': 'current', 'title': 'Current', 'artist': 'Artist', 'key': 'current|artist'},
        {'video_id': 'pending', 'title': 'Pending', 'artist': 'Artist', 'key': 'pending|artist'},
    ]
    # Seed the genuine predecessor's catalog. Current catalog mutation APIs
    # also update listening columns introduced after this schema version.
    with store.conn:
        for track in tracks:
            recording = store.conn.execute(
                'INSERT INTO recordings(identity_key,title,artist,created_at) VALUES (?,?,?,1)',
                (track['key'], track['title'], track['artist'])).lastrowid
            store.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES (?,?,?,?)',
                (track['video_id'], recording, track['title'], track['artist']))
            store.conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)',
                (recording, track['video_id']))
    session = Session(None, SimpleNamespace(fingerprint='checkpoint', tuning={}), account,
        'Radio', phase=Phase.APPENDING, playlist='RADIO', local_id=local_id,
        tracks=[tracks[0]], position=0, furthest=0, started_at=1, episode_id='cutover-episode')
    operation = AppendOperation(tracks[1])
    operation.write = PendingWrite(PlaylistEdit('add', ('current', 'pending'), track=tracks[1]), 10, None)
    session.edits.begin(operation)
    try:
        yield store, session
    finally:
        store.conn.close()


def cutover(store, *, through=94):
    previous = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    assert len(previous) == 93
    # The explicit historical DDL operates on physical owners. Retire only this
    # connection's admission projections before replacing those owners, then
    # restore supported reads through ordinary admission below.
    entity_names.clear_predecessor_reads(store.conn)
    migrate(store.conn, registry=registry.MIGRATIONS[:through])
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    assert history[:93] == previous
    assert [(row[0], row[1]) for row in history] == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS[:through]]
    store.init_schema(defer_metadata=True)
    assert [tuple(row) for row in store.conn.execute(
        'SELECT * FROM schema_migrations ORDER BY version')] == history


@pytest.mark.parametrize('through', [94, 100], ids=['v94', 'v100'])
@pytest.mark.parametrize('observed', ['saved', 'loaded'])
@pytest.mark.parametrize('flush', ['save', 'record_edit', 'unchanged'])
def test_same_controller_flushes_and_stops_after_cutover(predecessor, through, observed, flush):
    store, session = predecessor
    paused = {'value': False}
    if observed == 'loaded':
        RadioPersistence(store.settings).save(session)
    persistence = RadioPersistence(store.settings, paused=lambda: paused['value'])
    if observed == 'saved':
        persistence.save(session)
    original = json.loads(encode(session))
    paused['value'] = True
    if flush != 'unchanged':
        session.error = 'Pending readback retained during repair'
    persistence.save(session)
    pending = json.loads(encode(session))
    cutover(store, through=through)
    assert radio_storage.load(store.conn) == (original, (session.episode_id, 1))
    assert persistence.load()['error'] == pending['error']

    paused['value'] = False
    if flush == 'record_edit':
        persistence.record_edit(session.episode_id, session.edits.active)
    else:
        persistence.save(session)
    assert radio_storage.load(store.conn) == (pending, (session.episode_id, 2))
    assert persistence.transient is None

    session.request_stop()
    session.move(Phase.STOPPED)
    persistence.save(session)
    stopped, token = radio_storage.load(store.conn)
    assert stopped == json.loads(encode(session))
    assert token == (session.episode_id, 3)
    for field in ('identity', 'episode_id', 'playlist', 'local_id', 'tracks',
                  'pending', 'pending_sent', 'pending_result', 'pending_started_at'):
        assert stopped[field] == original[field]
    assert [tuple(row) for row in store.conn.execute(
        'SELECT position,video_id,state FROM radio_queue_items ORDER BY position')] == [
            (0, 'current', 'current'), (1, 'pending', 'pending')]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('replacement', ['save', 'stop', 'episode', 'predecessor_changed'])
def test_pre_cutover_controller_cannot_adopt_a_different_checkpoint(predecessor, replacement):
    store, session = predecessor
    paused = {'value': False}
    stale = RadioPersistence(store.settings, paused=lambda: paused['value'])
    stale.save(session)
    paused['value'] = True
    session.error = 'Transient journal must not replace another writer'
    stale.save(session)
    transient = stale.transient
    if replacement == 'predecessor_changed':
        state = json.loads(encode(session))
        state['error'] = 'Another predecessor writer'
        store.settings.set_setting('radio_current_session', json.dumps(state))
    cutover(store)
    if replacement != 'predecessor_changed':
        newer = RadioPersistence(store.settings)
        if replacement == 'stop':
            session.request_stop()
            session.move(Phase.STOPPED)
        elif replacement == 'episode':
            session.episode_id = 'newer-episode'
        newer.save(session)
    expected = radio_storage.load(store.conn)
    paused['value'] = False
    with pytest.raises(ValueError, match='changed.*worker'):
        stale.save(session)
    assert radio_storage.load(store.conn) == expected
    assert stale.transient == transient
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_controller_created_during_repair_can_save_into_an_empty_head(predecessor):
    store, session = predecessor
    paused = {'value': True}
    persistence = RadioPersistence(store.settings, paused=lambda: paused['value'])
    persistence.save(session)
    cutover(store)
    assert radio_storage.load(store.conn) == (None, None)
    paused['value'] = False
    persistence.save(session)
    assert radio_storage.load(store.conn) == (json.loads(encode(session)), (session.episode_id, 1))


def test_loaded_v1_checkpoint_can_resume_after_cutover(predecessor):
    store, session = predecessor
    legacy = json.loads(encode(session))
    legacy['version'] = 1
    legacy.pop('phase')
    store.settings.set_setting('radio_current_session', json.dumps(legacy))
    paused = {'value': True}
    persistence = RadioPersistence(store.settings, paused=lambda: paused['value'])
    assert persistence.load()['phase'] == Phase.APPENDING
    session.error = 'Retained while converting a v1 checkpoint'
    persistence.save(session)
    cutover(store)
    paused['value'] = False
    persistence.save(session)
    assert radio_storage.load(store.conn) == (json.loads(encode(session)), (session.episode_id, 2))


@pytest.mark.parametrize('flush', ['save', 'record_edit'])
def test_canonical_key_correction_keeps_same_controller_admission(predecessor, flush):
    store, session = predecessor
    paused = {'value': False}
    persistence = RadioPersistence(store.settings, paused=lambda: paused['value'])
    persistence.save(session)
    original = json.loads(encode(session))
    paused['value'] = True
    session.error = 'Retain uncertain append across canonical identity correction'
    persistence.save(session)
    cutover(store, through=100)
    with store.conn:
        key = original['tracks'][0]['key']
        recording = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()[0]
        store.conn.execute('INSERT INTO recording_aliases VALUES (?,?,?)', (key, recording, 'original_title'))
        alternate = store.conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) '
            'VALUES (?,?,?,1)', ('alternate|artist', 'Alternate', 'Artist')).lastrowid
        catalog.add_alias(store.conn, key, alternate, 'title_artist_match')
    projected, token = radio_storage.load(store.conn)
    corrected_key = projected['tracks'][0]['key']
    assert corrected_key != key
    assert token == (session.episode_id, 1)
    assert radio_storage.load_published(store.conn) == (original, token)

    paused['value'] = False
    if flush == 'record_edit':
        persistence.record_edit(session.episode_id, session.edits.active)
    else:
        persistence.save(session)
    resumed, token = radio_storage.load(store.conn)
    assert token == (session.episode_id, 2)
    assert resumed['error'] == session.error
    assert resumed['tracks'][0]['key'] == corrected_key
    assert resumed['tracks'][0]['video_id'] == original['tracks'][0]['video_id']
    for field in ('identity', 'episode_id', 'playlist', 'local_id', 'pending',
                  'pending_sent', 'pending_result', 'pending_started_at'):
        assert resumed[field] == original[field]
    assert persistence.transient is None
    session.request_stop()
    session.move(Phase.STOPPED)
    persistence.save(session)
    restarted = RadioPersistence(store.settings).load()
    assert restarted['phase'] == Phase.STOPPED
    assert restarted['tracks'][0]['key'] == corrected_key
    assert restarted['pending'] == original['pending']
    assert radio_storage.load(store.conn)[1] == (session.episode_id, 3)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
