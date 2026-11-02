"""Current HTTP readers over genuine installed listening owners before v100."""
from contextlib import closing, contextmanager
import sqlite3

from fastapi.testclient import TestClient
import pytest

from tests.conftest import seed_catalog_track
from yt_playlist.core import albums, artists, discovery_sources
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.preparation import subject_publication
from tests.core.test_subject_publication import ready
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app

DAY = 86400
EXACT = 1577880000  # 2020-01-01 at noon UTC.
NOW = 1700000000
SUBJECTS = ('play_event_subjects', 'listening_observation_subjects')


def installed(path, version, populated):
    store = Store(path)
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:version])
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Listener','credential',1)")
        store.conn.commit()
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN (?,?)", SUBJECTS).fetchall()
        if populated:
            seed_catalog_track(store, 'known|artist', 'Known', 'Artist', video_id='known')
            seed_catalog_track(store, 'coarse|artist', 'Coarse', 'Artist', video_id='coarse')
            with store.conn:
                for browse, key in (('MPRE_played', 'known|artist'), ('MPRE_coarse', 'coarse|artist'),
                                    ('MPRE_cold', None)):
                    album = albums.observe(store.conn, browse, title=browse, artist='Artist')
                    store.conn.execute("INSERT INTO account_albums(account_id,album_id,source) VALUES (3,?,'youtube')", (album,))
                    if key:
                        entry = store.conn.execute('SELECT e.id FROM catalog_entries e JOIN recordings r '
                            'ON r.id=e.recording_id WHERE r.identity_key=?', (key,)).fetchone()[0]
                        albums.link(store.conn, entry, browse, source='fixture')
                store.conn.execute("INSERT INTO listening_calendars VALUES (3,'UTC',1)")
                for key, video, at, generated in (('known|artist', 'known', EXACT, 0),
                                                 ('known|artist', 'known', EXACT + 3 * DAY, 1),
                                                 ('unresolved|artist', 'outside', EXACT + 4 * DAY, 0)):
                    store.conn.execute('INSERT INTO play_events(identity_id,identity_key,video_id,played_at,'
                        'is_generated,source) VALUES (3,?,?,?,?,?)', (key, video, at, generated, 'takeout'))
                for key in ('coarse|artist', 'unresolved|artist'):
                    recording = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
                    store.conn.execute('INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,'
                        'recording_id,source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
                        "VALUES (3,?,?,?,'2020-01-02','2020-01-02','independent','UTC','calendar_day',1,2)",
                        (key, key, recording[0] if recording else None))
                store.conn.execute('INSERT INTO listening_capture_receipts VALUES (5,3,?)', (EXACT + 5 * DAY,))
                store.conn.execute('INSERT INTO listening_observations(identity_id,identity_key,recording_id,day,source_day,'
                    'source,precision,first_observed_at,last_observed_at,capture_id,capture_item_id) '
                    "VALUES (3,'unresolved|artist',NULL,'2020-01-06','2020-01-06','ytm_history','legacy_unknown',?,?,5,50)",
                    (EXACT + 5 * DAY, EXACT + 5 * DAY))
                artist = artists.ensure(store.conn, 'Artist', provider='youtube', provider_id='UC_starter')
                store.conn.execute('INSERT INTO discovered_artists(artist,artist_id,score,thumbnail,found_at) '
                                   "VALUES (?,?,3,'portrait',1)", ('@artist:' + str(artist), artist))
                discovery_sources.retain_artist(store.conn, artist, 'artist_start:UC_starter', 3)
                recording = store.conn.execute("SELECT id FROM recordings WHERE identity_key='known|artist'").fetchone()[0]
                discovery_sources.retain(store.conn, recording, 'artist:UC_starter', 'known', 1)
        validate_version(store.conn, version)
        return store
    except BaseException:
        store.conn.close()
        raise


def persisted(conn, *, home_visits=False):
    # Real Home visits intentionally tick card rotation while installed owners
    # remain writable. Every other persisted value and schema object must match.
    dump = tuple(row for row in conn.iterdump() if not (home_visits and
                 row.startswith('INSERT INTO "rec_impressions" VALUES(\'card\',')))
    return conn.execute('PRAGMA main.schema_version').fetchone()[0], dump


@contextmanager
def deferred(path, version, populated):
    setup = installed(path, version, populated)
    before = persisted(setup.conn)
    setup.conn.close()
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        assert not store.activity_paused and not store._schema_recovery_pending
        assert persisted(store.conn) == before
        yield store
    finally:
        store.conn.close()


def web(store):
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: NOW, background_work=False),
                      base_url='http://127.0.0.1')


def read_startup(client):
    response = client.get('/home/onboard/status')
    assert response.status_code == 200 and isinstance(response.json()['setup'], dict)
    response = client.get('/')
    assert response.status_code == 200 and '<html' in response.text.lower()
    assert client.get('/home/growth').status_code == 200


def listening_state(store):
    return {name: [dict(row) for row in store.conn.execute('SELECT * FROM ' + name + ' ORDER BY id')]
            for name in SUBJECTS}


@pytest.mark.parametrize('version', [79, 88, 99])
@pytest.mark.parametrize('populated', [False, True], ids=['empty', 'populated'])
def test_startup_reads_installed_listening_without_mutating_it(tmp_path, version, populated):
    with deferred(tmp_path / 'installed.db', version, populated) as store:
        before = persisted(store.conn,home_visits=True)
        client = web(store)
        read_startup(client)
        read_startup(client)
        with store.read_view() as reader:
            state = listening_state(reader)
            assert len(state['play_event_subjects']) == (3 if populated else 0)
            assert len(state['listening_observation_subjects']) == (3 if populated else 0)
            expected = {'MPRE_played': EXACT + 3 * DAY, 'MPRE_coarse': EXACT + 2 * DAY, 'MPRE_cold': None}
            assert reader.collection.saved_albums_recency(include_generated=True) == (expected if populated else {})
            assert [row['browse_id'] for row in reader.discovery.starter_artists()] == (['UC_starter'] if populated else [])
            assert [row['key'] for row in reader.discovery.starter_tracks()] == (['known|artist'] if populated else [])
            if populated:
                assert reader.collection.saved_albums_recency() == {
                    **expected, 'MPRE_played': EXACT, 'MPRE_coarse': EXACT + DAY}
                unknown = state['play_event_subjects'][-1]
                assert unknown['identity_key'] == 'unresolved|artist' and unknown['recording_id'] is None
                assert {row['precision'] for row in state['listening_observation_subjects']} == {
                    'calendar_day', 'legacy_unknown'}
                captured = state['listening_observation_subjects'][-1]
                assert captured['day'] == '2020-01-06' and captured['timezone'] is None
                assert captured['capture_id'] == 5 and captured['recording_id'] is None
            assert all(row['account_id'] == 3 for rows in state.values() for row in rows)
        for view in SUBJECTS:
            with pytest.raises(sqlite3.OperationalError, match='view'):
                store.conn.execute('DELETE FROM ' + view)
            store.conn.rollback()
        assert persisted(store.conn,home_visits=True) == before
        from yt_playlist.web.routes.home import ROTATING_CARDS
        assert [tuple(row) for row in store.conn.execute(
            "SELECT item_key,views,last_shown FROM rec_impressions WHERE surface='card' ORDER BY item_key")] == [
                (card,2,NOW) for card in sorted(ROTATING_CARDS)]
        store.settings.set_setting('installed-write', 'accepted')
        store.history.record_play_event(3,'continuing|unknown','continuing',NOW,source_timezone='UTC')
        assert store.settings.get_setting('installed-write') == 'accepted'
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
        with store.read_view() as reader:
            assert listening_state(reader)['play_event_subjects'][-1]['identity_key'] == 'continuing|unknown'


@pytest.mark.parametrize('version', [79, 99])
def test_pinned_listening_reader_survives_explicit_publication_and_new_readers_select_it(tmp_path, version):
    with deferred(tmp_path / 'cutover.db', version, True) as store:
        client = web(store)
        # Publication is explicit and leaves installed writers/history intact.
        # Old read adapters retire only with their separately verified cleanup.
        with store.read_view() as pinned, closing(sqlite3.connect(store._db_path)) as job:
            job.row_factory = sqlite3.Row
            job.execute('PRAGMA foreign_keys=ON')
            before = listening_state(pinned)
            starters = pinned.discovery.starter_artists(), pinned.discovery.starter_tracks()
            assert set(SUBJECTS) <= set(pinned.conn._entity_read_views)
            identity = ready(job)
            subject_publication.publish(job,identity=identity)
            assert subject_publication.selected(job) == identity
            assert listening_state(pinned) == before
            assert (pinned.discovery.starter_artists(), pinned.discovery.starter_tracks()) == starters
            assert pinned.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
            read_startup(client)
            with store.read_view() as reader:
                assert subject_publication.selected(reader.conn) == identity
                assert (reader.discovery.starter_artists(), reader.discovery.starter_tracks()) == starters
                # Prove these reads select retained custody rather than silently
                # falling back to the identical installed event/observation rows.
                def mask(operation,owner,*unused):
                    return (sqlite3.SQLITE_IGNORE if operation == sqlite3.SQLITE_READ and
                            owner in ('play_events','listening_observations') else sqlite3.SQLITE_OK)
                reader.conn.set_authorizer(mask)
                try:
                    after = listening_state(reader)
                    for name, originals in before.items():
                        expected = [{field: (row['identity_key'] if row['recording_id'] is None else None)
                                     if field == 'unresolved_subject_key' else None
                                     if field == 'membership_ref_id' else row[field]
                                     for field in after[name][0]} for row in originals]
                        assert after[name] == expected
                finally:
                    reader.conn.set_authorizer(None)
                assert reader.collection.saved_albums_recency(include_generated=True)['MPRE_cold'] is None
        store.history.record_play_event(3,'continuing|unknown','continuing',NOW,source_timezone='UTC')
        assert subject_publication.selected(store.conn) is None
        with store.read_view() as reader:
            assert listening_state(reader)['play_event_subjects'][-1]['identity_key'] == 'continuing|unknown'
        with closing(sqlite3.connect(store._db_path)) as installed:
            validate_version(installed,version)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
