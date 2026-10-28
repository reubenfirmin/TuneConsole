"""The personal portrait uses observed behavior without training exclusions or guessed time."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tests.conftest import seed_catalog_track
from tests.rec.test_summary_maintenance import worker_for, tick
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.library import live_plays, ratings
from yt_playlist.rec import listening_analytics as analytics, maintenance
from yt_playlist.rec.choice_capture import CaptureQueue
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo
from yt_playlist.web.app import create_app

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def listening_enabled(monkeypatch, store):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', '1')
    store.settings.set_setting('listening_timezone', 'UTC')


def seed(store):
    iid = store.identities.upsert_identity('Listener', 'fixture', None, True)
    for key, artist, genre in [('a', 'A', 'Techno'), ('b', 'A', 'Techno'), ('c', 'B', 'Jazz')]:
        seed_catalog_track(store, key, key.upper(), artist, video_id='v'+key, genre=genre)
    return iid


def total(payload, field):
    return sum(row[payload['columns'].index(field)] for row in payload['rows'])


def event(store, iid, kind, at, position=None, *, playlist='LM', payload=None, video='va'):
    store.player_events.record_player_event(iid, kind, video, position, 1000, playlist,
                                           json.dumps(payload or {}), at)


def test_empty_generation_and_serving_never_calculate(store, monkeypatch):
    app = create_app(store, lambda: {}, now_fn=lambda: NOW)
    with TestClient(app) as client:
        assert client.get('/charts/listening-data').status_code == 202
        payload = analytics.build(store, NOW)
        assert payload['rows'] == []
        encoded = ListeningAnalyticsRepo(store).encoded()
        monkeypatch.setattr(analytics, 'calculate', lambda *a: pytest.fail('calculated on request'))
        response = client.get('/charts/listening-data')
        assert response.status_code == 200 and response.text == encoded
        assert response.headers['cache-control'] == 'no-store'


def test_generated_plays_coarse_evidence_and_discovery_use_complete_history(store):
    iid = seed(store)
    store.playlists.upsert_playlist(iid, 'mix', 'Mix', 1, '', 1)
    store.playlists.mark_generated_playlist('mix')
    store.history.record_history_plays(iid, NOW-86400*10, ['a'], source_timezone='UTC')
    store.history.record_play_event(iid, 'a', 'va', NOW-86400, playlist_ytm_id='mix')
    store.history.record_play_event(iid, 'b', 'vb', NOW-86000, playlist_ytm_id='OLAK5uy_album')
    store.history.record_play_event(iid, 'c', 'vc', NOW-85000, playlist_ytm_id='LM')
    payload = analytics.build(store, NOW)
    assert total(payload, 'plays') == 3
    assert total(payload, 'sightings') == 1  # live calendar observations do not double-count plays
    assert total(payload, 'new_tracks') == 2
    assert total(payload, 'new_artists') == 1
    assert total(payload, 'rated') == 0  # mutable play-event status is not historical evidence
    by_format = {row[3]: row for row in payload['rows'] if row[4]}
    assert set(by_format) == {'mixes', 'albums', 'liked'}
    assert store.conn.execute('SELECT is_generated FROM play_events WHERE identity_key=?', ('a',)).fetchone()[0]


def test_discovery_baseline_excludes_the_initial_capture_and_survives_partitioning(store):
    iid = seed(store)
    # A large first sync is still the start of a record, even with an old sighting.
    for index in range(30):
        key = f'baseline-{index}'
        seed_catalog_track(store, key, key, f'Artist {index}', video_id=f'v-{index}')
    store.history.record_history_plays(iid, NOW-60*86400, ['baseline-0'], source_timezone='UTC')
    for index in range(100):
        store.history.record_play_event(iid, f'baseline-{index%30}', f'v-{index%30}', NOW-40*86400+index)
    assert analytics.build(store, NOW)['discovery_from'] is None
    # Independent earlier listening days establish a baseline; no rate is inferred
    # here. The browser additionally requires repeat listening in its selected dates.
    for ago in range(39, 26, -1):
        store.history.record_history_plays(iid, NOW-ago*86400, ['baseline-0'], source_timezone='UTC')
    store.history.record_play_event(iid, 'a', 'va', NOW-86400)
    payload = analytics.build(store, NOW)
    assert payload['discovery_from'] == '2026-09-27'
    assert ListeningAnalyticsRepo(store).prepared()['discovery_from'] == '2026-09-27'
    assert payload['catalog']['baseline-0']['cohort']['first'] == '2026-08-25'


def test_observed_seconds_exclude_pause_mute_seek_missing_samples_and_duplicate_exit(store):
    iid = seed(store)
    samples = [('tick', 0, 0, {}), ('tick', 30, 30, {}),
        ('state', 40, 40, {'state': 'paused'}), ('state', 60, 40, {'state': 'playing'}),
        ('tick', 90, 70, {}), ('volume', 100, 80, {'volume': 0}),
        ('tick', 130, 110, {}), ('volume', 140, 120, {'volume': 1}),
        ('tick', 170, 150, {}), ('tick', 180, 500, {}),  # seek
        ('tick', 300, 620, {}),  # missing heartbeats
        ('track_exit', 310, 630, {}), ('bye', 310, 630, {})]
    for kind, at, position, payload in samples:
        event(store, iid, kind, NOW-1000+at, position, payload=payload)
    payload = analytics.build(store, NOW)
    assert total(payload, 'seconds') == 120
    assert total(payload, 'plays') == 0  # samples never invent play events
    assert {row[3] for row in payload['rows']} == {'liked'}


def test_midnight_sample_is_split_and_radio_context_is_durable(store):
    iid = seed(store)
    store.settings.set_setting('radio_session_playlist_ids', '["saved-radio"]')
    midnight = NOW - 12*3600
    event(store, iid, 'tick', midnight-10, 0, playlist='saved-radio')
    event(store, iid, 'tick', midnight+20, 30, playlist='saved-radio')
    payload = analytics.build(store, NOW)
    assert [(row[0],row[3],row[payload['columns'].index('seconds')]) for row in payload['rows']] == [
        ('2026-10-23', 'radio', 10), ('2026-10-24', 'radio', 20)]


def test_new_start_and_positionless_exit_break_elapsed_intervals(store):
    iid = seed(store)
    for kind, at, position in [('tick',0,0),('tick',10,10),
        ('listen_start',15,None),('tick',20,20),('tick',30,30),
        ('track_exit',35,None),('tick',40,40),('tick',50,50)]:
        event(store,iid,kind,NOW-100+at,position)
    assert total(analytics.build(store,NOW),'seconds') == 30


def test_local_midnight_and_dst_do_not_borrow_time_from_another_day(store):
    iid=seed(store)
    store.settings.set_setting('listening_timezone','America/New_York')
    midnight=datetime(2026,3,9,4,tzinfo=timezone.utc).timestamp()
    event(store,iid,'tick',midnight-10,0)
    event(store,iid,'tick',midnight+20,30)
    payload=analytics.build(store,midnight+60)
    assert payload['timezone']=='America/New_York'
    assert [(r[0],r[payload['columns'].index('seconds')]) for r in payload['rows']]==[
        ('2026-03-08',10),('2026-03-09',20)]


def test_confirmed_action_history_survives_clear_and_native_or_sync_state_is_not_an_action(store):
    iid=seed(store)
    ratings.record_observations(store,iid,{'va':'LIKE'},source='sync',now=NOW-500)
    ratings.record_observations(store,iid,{'vb':'LIKE'},source='action',now=NOW-400)
    ratings.record_confirmed(store,iid,'va','DISLIKE',NOW-300)
    ratings.record_confirmed(store,iid,'va','INDIFFERENT',NOW-200)
    ratings.record_confirmed(store,iid,'va','LIKE',NOW-100)
    ratings.record_confirmed(store,iid,'va','LIKE',NOW-90)
    payload=analytics.build(store,NOW)
    assert (total(payload,'hearts'),total(payload,'skulls'),total(payload,'clears'))==(1,1,1)
    actions=list(store.conn.execute('SELECT previous_status,status FROM listening_rating_actions ORDER BY id'))
    assert [tuple(a) for a in actions]==[('LIKE','DISLIKE'),('DISLIKE','INDIFFERENT'),('INDIFFERENT','LIKE')]


def test_publication_uses_compact_manifest_and_preserves_inflight_previous_chunks(store):
    iid=seed(store)
    store.history.record_play_event(iid,'a','va',NOW-100)
    payload=analytics.build(store,NOW)
    repo=ListeningAnalyticsRepo(store)
    manifest=repo.prepared()
    assert 'rows' not in manifest and 'catalog' not in manifest
    assert len(repo.encoded().encode())<32768
    first=manifest['chunks'][0]['key']
    assert json.loads(repo.encoded('listening_chunk:'+first))['rows']==payload['rows']
    store.history.record_play_event(iid,'b','vb',NOW-20)
    analytics.build(store,NOW)
    assert repo.encoded('listening_chunk:'+first)  # an already opened manifest still works
    assert all(c['bytes']<512*1024 for c in repo.prepared()['chunks'])
    current=repo.prepared()['chunks'][0]['key']
    with store.conn:
        store.conn.execute('DELETE FROM play_events')
        store.conn.execute('DELETE FROM listening_observations')
    analytics.build(store,NOW)
    assert not repo.prepared()['available']
    assert repo.encoded('listening_chunk:'+current)  # one previous generation remains readable
    analytics.build(store,NOW)
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface LIKE 'listening_chunk:%'").fetchone()


def test_rating_start_is_frozen_queued_and_never_borrowed_from_another_play(store):
    iid = store.identities.upsert_identity('Listener', 'fixture', None, True)
    offers = []
    store.choice_capture = CaptureQueue(lambda capture: offers.append(capture) or True)
    ctx = SimpleNamespace(store=store)
    msg = dict(title='Song', artist='Artist', videoId='video', likeStatus='INDIFFERENT')
    assert live_plays.handle_play_event(ctx, msg, NOW-5000)
    assert not store.player_events.player_events_since(0, 'listen_start')  # request only queues
    for capture in offers: capture.run(); capture.close()
    offers.clear()
    ratings.record_confirmed(store, iid, 'video', 'LIKE', NOW-4000)
    assert not live_plays.handle_play_event(ctx, {**msg, 'likeStatus':'LIKE'}, NOW-3999)
    assert live_plays.handle_play_event(ctx, msg, NOW-1000)
    for capture in offers: capture.run(); capture.close()
    ratings.record_confirmed(store, iid, 'video', 'DISLIKE', NOW-100)
    payload = analytics.build(store, NOW)
    assert total(payload, 'plays') == 2
    assert total(payload, 'rated') == 1 and total(payload, 'liked') == 1
    assert total(payload, 'hearts') == 1 and total(payload, 'skulls') == 1


def test_worker_schedules_telemetry_separately_and_throttles_it(store, monkeypatch):
    iid = seed(store)
    worker = worker_for(store, monkeypatch, [NOW])
    tick(worker)
    assert ListeningAnalyticsRepo(store).encoded()
    assert not worker.projections.active
    event(store, iid, 'tick', NOW-30, 0)
    worker.projections.poll()
    assert not worker.jobs.has('analytics')  # at most one generation every two minutes
    worker.projections.retry_at.clear()
    worker.projections.poll()
    assert worker.jobs.has('analytics') and not worker.jobs.has('trends')
    assert not worker.jobs.visible
    original = analytics.build

    def build_without_model_spinner(*args):
        assert not worker.busy
        return original(*args)

    monkeypatch.setattr(analytics,'build',build_without_model_spinner)
    worker._run_pending()
    assert worker.projections.failures['analytics'] == 0


def test_conflicting_sources_keep_last_good_and_scans_require_private_reader(store, monkeypatch):
    iid = seed(store)
    assert analytics.build(store, NOW)
    encoded = ListeningAnalyticsRepo(store).encoded()
    original = analytics.calculate

    def conflict(reader, now):
        assert reader._read_only and reader.conn is not store.conn
        result = original(reader, now)
        event(store, iid, 'tick', NOW, 10)
        return result

    monkeypatch.setattr(analytics, 'calculate', conflict)
    assert analytics.build(store, NOW) is None
    assert ListeningAnalyticsRepo(store).encoded() == encoded
    with pytest.raises(RuntimeError, match='private worker snapshot'):
        list(ListeningAnalyticsRepo(store).catalog_rows())


def test_snapshot_scans_are_bounded_and_cancellable(store, monkeypatch):
    iid = seed(store)
    with store.conn:
        store.conn.executemany('INSERT INTO player_events(identity_id,kind,video_id,position,at) VALUES (?,?,?,?,?)',
            [(iid, 'tick', 'va', n, NOW-1000+n) for n in range(600)])
    with store.read_snapshot() as snapshot:
        batches = list(ListeningAnalyticsRepo(snapshot.store).player(NOW))
        assert [len(batch) for batch in batches] == [256,256,88]
        repo = ListeningAnalyticsRepo(snapshot.store)
        connection, lock = repo.conn, repo._lock
        state = {'locked': False, 'closed': False, 'fetches': 0}

        class Guard:
            def __enter__(self):
                lock.acquire()
                state['locked'] = True

            def __exit__(self, *args):
                state['locked'] = False
                lock.release()

        class CheckedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchmany(self, size):
                assert state['locked']
                state['fetches'] += 1
                return self.cursor.fetchmany(size)

            def close(self):
                assert state['locked']
                state['closed'] = True
                self.cursor.close()

        class CheckedConnection:
            def execute(self, *args):
                assert state['locked']
                return CheckedCursor(connection.execute(*args))

        repo._lock, repo.conn = Guard(), CheckedConnection()
        stream = repo.player(NOW)
        assert len(next(stream)) == 256
        assert not state['locked']  # Consumer calculation must run outside the guard.

        def cancelled():
            raise WorkBudgetExceeded('cancelled between batches')

        monkeypatch.setattr('yt_playlist.repos.listening_analytics.check_cancellable_work', cancelled)
        with pytest.raises(WorkBudgetExceeded, match='between batches'):
            next(stream)
        assert state == {'locked': False, 'closed': True, 'fetches': 1}
    assert ('player_events','') not in maintenance.DEPENDENCIES['analytics']
