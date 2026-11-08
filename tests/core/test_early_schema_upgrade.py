"""Literal early SQL crosses each real owner once, with independent contracts."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_current_schema import objects
from tests.catalog_migration_support import catalog_reader
from tests.genre_catalog_support import bound_catalog
from tests.metadata_provenance_support import metadata_provenance
from tests.regular_schema_support import regular_expectation, assert_regular_expectation
from tests.test_audio_vibes import sample
from yt_playlist.core import catalog
from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
from yt_playlist.core.migrations import Migration, MigrationError, current, registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store
from yt_playlist.util import genre_catalog
from yt_playlist.providers.base import MetadataObservation


FIXTURES = Path(__file__).parents[1] / 'fixtures/migrations'


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL and admission owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_home(tmp_path_factory):
    vocabulary = {name: getattr(genre_catalog, name) for name in (
        'catalog', 'family_labels', 'builtin_names', 'legacy_names', 'legacy_entries')}
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('early-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz'), ('blues', 'Blues')))
        yield vocabulary


@pytest.fixture(scope='module')
def early(tmp_path_factory):
    path = tmp_path_factory.mktemp('early-schema') / 'legacy.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript((FIXTURES / 'early_core.sql').read_text())
        conn.executescript('''
            CREATE TABLE play_events (id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),
                identity_key TEXT NOT NULL,video_id TEXT,played_at REAL NOT NULL,playlist_ytm_id TEXT,like_status TEXT);
            INSERT INTO play_events VALUES (9,3,'original title|artist','original-video',100,NULL,NULL);
            CREATE TABLE discovered_artists (artist TEXT PRIMARY KEY,score REAL,because TEXT,
                fits TEXT,thumbnail TEXT,found_at REAL,last_shown REAL);
            CREATE TABLE discovered_albums (browse_id TEXT PRIMARY KEY,artist TEXT,title TEXT,
                year TEXT,thumbnail TEXT,found_at REAL,last_shown REAL);
            INSERT INTO discovered_artists(artist,score) VALUES ('Old Artist',0.3);
            CREATE TABLE rec_mode_impressions (epoch INTEGER NOT NULL,lane TEXT NOT NULL,
                mode_id INTEGER NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(epoch,lane));
            CREATE TABLE rec_mode_picks (playlist_id INTEGER PRIMARY KEY,mode_id INTEGER NOT NULL,created_at REAL NOT NULL);
        ''')
        store = Store(path)
        assert not hasattr(store, 'tracks')
        try:
            yield dict(conn=conn, path=path, store=store, version=0)
        finally:
            store.conn.close()


def test_literal_early_input_keeps_original_ids_and_the_unconstrained_play_ledger(early):
    conn = early['conn']
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchone()
    assert tuple(conn.execute('SELECT id,title,artist FROM tracks').fetchone()) == (17, 'Original title', 'Artist')
    assert tuple(conn.execute('SELECT * FROM playlist_tracks').fetchone()) == (8, 17, 0)
    assert 'CHECK' not in conn.execute("SELECT sql FROM sqlite_schema WHERE name='play_events'").fetchone()[0]


@pytest.fixture(scope='module')
def empty_reference(isolated_home):
    # A genuine empty replay is the oracle for historical layouts. Today's
    # assembled destination intentionally is not a definition of early DDL.
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        yield {'conn': conn, 'vocabulary': isolated_home}


@pytest.fixture(scope='module', params=range(1, 103))
def target(request):
    return request.param


def test_actual_empty_edge_independently_establishes_its_historical_contract(empty_reference, target):
    # Exact seed parity with the compiled fresh destination needs the actual
    # shipped vocabulary. Populated conversion checks can use bounded inputs.
    with pytest.MonkeyPatch.context() as patch:
        for name, operation in empty_reference['vocabulary'].items():
            patch.setattr(genre_catalog, name, operation)
        runner.migrate(empty_reference['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    empty_reference['expected'] = objects(empty_reference['conn'])
    assert list(map(tuple, empty_reference['conn'].execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS[:target]]
    assert not empty_reference['conn'].execute('PRAGMA foreign_key_check').fetchone()


def seed_owner_inputs(state, target):
    conn = state['conn']
    if target == 1:
        with conn:
            conn.execute('UPDATE tracks SET created_at=1,first_enriched_at=5,last_enriched_at=5 WHERE id=17')
            conn.execute("INSERT INTO discovered_tracks(identity_key,video_id,title,artist,genre,year,source_browse_id,found_at,bpm,last_enriched) "
                "VALUES ('cold','v1','Cold','Artist','House','2020','radio:seed',100,128,200)")
            conn.executemany('INSERT INTO settings VALUES (?,?)', [
                ('personal-setting', 'retain'), ('rec_param:onboard_coverage_min', '0.9'),
                ('rec_param:onboard_library_min', '200'), ('rec_param:onboard_feedback_min', '15'),
                ('onboard_dismissed', '1'), ('last_sync_at', '1000')])
            conn.execute('INSERT INTO history_snapshots VALUES (1,3,100)')
            conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'original title|artist')")
    if target in (8, 9):
        installed_sql = (FIXTURES / 'play_events_subject_v8.sql').read_text().strip()
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='play_events_subject'").fetchone()[0] == installed_sql
        if target == 8:
            with conn:
                conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'song|artist')")
                conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('known',1)")
                for event_id, video in enumerate(('', '   ', None, 'known', 'unknown'), 30):
                    conn.execute('INSERT INTO play_events(id,identity_id,identity_key,video_id,played_at,source,is_generated) '
                        "VALUES (?,3,'song|artist',?,?,'live',1)", (event_id, video, event_id))
                conn.execute("INSERT INTO rec_weights VALUES ('genre:house',1.4,1)")
                conn.execute("INSERT INTO rec_vectors VALUES ('song|artist',x'01020304')")
            state['raw_events'] = list(map(tuple, conn.execute(
                'SELECT id,raw_identity_key,video_id,played_at,source,is_generated FROM play_events ORDER BY id')))
        runner.validate_version(conn, target)
    if target == 31:
        with conn:
            conn.executemany('INSERT INTO play_events(identity_id,identity_key,video_id,played_at,is_generated,source) '
                "VALUES (3,'original title|artist','original-video',?,?,'live')", [(1, None), (2, 0), (3, 1)])
        state['events'] = list(map(tuple, conn.execute(
            'SELECT id,video_id,played_at,is_generated FROM play_events ORDER BY id')))
        state['calendar'] = list(map(tuple, conn.execute('SELECT event_id,day FROM play_event_calendar ORDER BY event_id')))
        state['sources'] = list(map(tuple, conn.execute('SELECT event_id,source FROM play_event_sources ORDER BY event_id')))
    if target == 7:
        state['duplicates'] = {}
        with conn:
            for reverse in (False, True):
                for values in (('Rock', 'Pop'), ('Rock', '')):
                    offset = len(state['duplicates']) * 10
                    first, second, video = 300 + offset, 304 + offset, f'duplicate-{offset}'
                    rows = [(first, values[0], 10), (second, values[1], 20)]
                    if reverse:
                        rows.reverse()
                    for track, value, at in rows:
                        conn.execute('INSERT INTO tracks(id,identity_key,video_id,title,artist,genre) '
                            'VALUES (?,?,?,?,?,?)', (track, f'key-{track}', video, 'Duplicate song', 'Artist', value))
                        conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,'
                            'resolved_value,updated_at) VALUES (?,\'genre\',\'[]\',1,?,?)', (track, value, at))
                    state['duplicates'][reverse, values] = (video, second)
    if target == 14:
        with conn:
            conn.execute("INSERT INTO album_tracklists VALUES ('MPRE','Album','Artist',NULL,'[\"first|artist\",\"second|artist\"]')")
    if target == 19:
        with conn:
            conn.execute("INSERT INTO recordings(id,identity_key,genre) VALUES (200,'legacy','Rock')")
            conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,orig_title) VALUES ('metadata-edited',200,'Edited','Original')")
            conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (200,200,'metadata-edited')")
            conn.execute("INSERT INTO entry_field_choices VALUES (200,'title','\"Edited\"','user')")
        before = list(conn.iterdump())

        def interrupted(connection, context):
            registry.MIGRATIONS[19].apply(connection, context)
            raise RuntimeError('interrupted metadata migration')

        with pytest.raises(MigrationError, match='interrupted metadata'):
            runner.migrate(conn, registry=(*registry.MIGRATIONS[:19],
                Migration(20, registry.MIGRATIONS[19].name, interrupted)), clock=lambda: 2.)
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_decisions'").fetchone()
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 19
        assert list(conn.iterdump()) == before
    if target == 78:
        with catalog_reader(state['path'], 78) as store:
            entry = store.tracks.upsert_track('rename-video', 'Song', 'First, Second', None, 200,
                credits=[{'name': 'First', 'id': 'first'}, {'name': 'Second', 'id': 'second'}])
        state['credits'] = entry
        state['credit_rows'] = list(map(tuple, conn.execute('SELECT * FROM track_artists WHERE entry_id=? ORDER BY position', (entry,))))
        state['credit_selections'] = list(map(tuple, conn.execute('SELECT * FROM entry_artist_selections WHERE entry_id=?', (entry,))))
        state['credit_changes'] = conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0]
    if target == 69:
        state['store']._bind_repositories()
        assert not state['store']._compact_group_files
        state['old_groups'] = state['store'].taste_groups
    if target == 49:
        with catalog_reader(state['path'], 49) as store:
            for i, value in enumerate(([{'name': 'Metal'}], [])):
                store.enrichment_state.record_metadata_observations(17, 'fixture', [MetadataObservation(
                    'tags', value, 'artist', 'artist-id', 'reported',
                    match_evidence={'method': 'title_artist_match', 'input_title': 'Original title'})],
                    run_id=str(i), now=i)
        state['findings'] = list(map(tuple, conn.execute('SELECT * FROM metadata_findings ORDER BY id')))
        before = list(conn.iterdump())

        def interrupted_profiles(connection, context):
            registry.MIGRATIONS[49].apply(connection, context)
            raise RuntimeError('interrupted profiles upgrade')

        with pytest.raises(MigrationError, match='interrupted profiles'):
            runner.migrate(conn, registry=(*registry.MIGRATIONS[:49],
                Migration(50, registry.MIGRATIONS[49].name, interrupted_profiles)), clock=lambda: 2.)
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 49
        assert list(conn.iterdump()) == before
    if target == 60:
        value = sample()
        value['video_id'] = 'original-video'
        evidence = {'source': 'audio_measurement', 'sample_ids': [value['id']],
            'fits': {'relax': {'score': .8, 'support': 1.}}}
        profile = {'vibes': {'fits': {'relax': {'score': .8, 'support': 1., 'source': 'audio_measurement'}}}}
        with conn:
            conn.execute('INSERT INTO audio_vibe_samples VALUES (?,?,?,?,?)',
                (value['id'], value['run_id'], value['point'], json.dumps(value), 10))
            conn.execute('INSERT INTO rec_vibe_tracks VALUES (?,?,?,?,?)',
                ('original title|artist', None, 'psybient', json.dumps(evidence), 'old-model'))
            conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                ('original title|artist', json.dumps(profile), 'old', 'digest', 20))
            conn.execute('INSERT INTO audio_session_labels VALUES (?,?,?,?,?,?)',
                (value['run_id'], 'retained-taxonomy', json.dumps([value['id']]), 'null', '["relax"]', 11))
        state['sample'] = value
    if target == 79:
        # Check the rename before additional ordinary writes create legitimate
        # preparation evidence in this shared populated journey.
        assert conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] == state['credit_changes']
        assert list(map(tuple, conn.execute('SELECT * FROM metadata_findings ORDER BY id'))) == state['findings']
        with catalog_reader(state['path'], 79) as store:
            store.init_schema(defer_metadata=True)
            assert store.musical_profiles.pending()
            assert store.prepared_content.has_pending()
            before = list(store.conn.iterdump())
            store.init_schema(defer_metadata=True)
            assert list(store.conn.iterdump()) == before
            from tests.core.test_metadata_owner_cutover import seed

            entry, _, key = seed(store, title='Cutover song')
            state['cutover'] = (entry, key, dict(store.conn.execute(
                'SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()))
        state['credit_changes'] = conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0]
    if target == 99:
        before_changes = conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0]
        assert before_changes == state['credit_changes'] + 4
        with conn:
            conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at,source) "
                "VALUES (3,'original title|artist','original-video',20,'takeout')")
            conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at,source) "
                "VALUES (3,'original title|artist','unknown-subject',30,'live')")
            conn.execute('INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,'
                'recording_id,source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
                "VALUES (3,'Raw title','original title|artist',17,'2020-01-01','2020-01-01',"
                "'independent','UTC','calendar_day',1,2)")
        # Each exact play adds its event, source and calendar evidence, and
        # the coarse observation adds one independently captured input.
        assert conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] == before_changes + 7
        state['credit_changes'] += 7
        state['listening'] = {table: [dict(row) for row in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
            for table in ('play_events', 'play_event_sources', 'play_event_calendar', 'listening_observations')}
        state['events'] = list(map(tuple, conn.execute('SELECT id,video_id,played_at,is_generated FROM play_events ORDER BY id')))
        state['calendar'] = list(map(tuple, conn.execute('SELECT event_id,day FROM play_event_calendar ORDER BY event_id')))
        state['sources'] = list(map(tuple, conn.execute('SELECT event_id,source FROM play_event_sources ORDER BY event_id')))
        before = list(conn.iterdump())

        def interrupted_listening(connection, context):
            registry.MIGRATIONS[99].apply(connection, context)
            raise RuntimeError('interrupted listening conversion')

        with pytest.raises(MigrationError, match='interrupted listening conversion'):
            runner.migrate(conn, registry=(*registry.MIGRATIONS[:99],
                replace(registry.MIGRATIONS[99], apply=interrupted_listening)), clock=lambda: 2.)
        assert list(conn.iterdump()) == before


    if target == 101:
        before = list(conn.iterdump())

        def interrupted_facade(connection, context):
            registry.MIGRATIONS[101].apply(connection, context)
            raise RuntimeError('interrupted facade retirement')

        with pytest.raises(MigrationError, match='interrupted facade retirement'):
            runner.migrate(conn, registry=(*registry.MIGRATIONS[:101],
                replace(registry.MIGRATIONS[101], apply=interrupted_facade)), clock=lambda: 2.)
        assert list(conn.iterdump()) == before


def assert_originals(state, target):
    conn = state['conn']
    if target < 103:
        assert tuple(conn.execute('SELECT id,title,artist FROM tracks WHERE id=17').fetchone()) == (17, 'Original title', 'Artist')
        assert tuple(conn.execute('SELECT * FROM playlist_tracks').fetchone()) == (8, 17, 0)
    else:
        assert tuple(conn.execute('SELECT e.id,r.title,r.artist FROM catalog_entries e '
            'JOIN recordings r ON r.id=e.recording_id WHERE e.id=17').fetchone()) == (17, 'Original title', 'Artist')
        assert tuple(conn.execute('SELECT * FROM playlist_items').fetchone()) == (8, 'original-video', 0)
    assert conn.execute("SELECT value FROM settings WHERE key='user-setting'").fetchone()[0] == 'keep me'
    assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain'
    if target >= 2:
        relation = 'tracks' if target < 103 else 'recordings'
        assert tuple(conn.execute('SELECT first_enriched_at,last_enriched_at FROM '+relation+' WHERE id=17').fetchone()) == (None, None)
    if target < 33:
        assert tuple(conn.execute('SELECT snapshot_id,identity_key FROM history_items').fetchone()) == (1, 'original title|artist')
    else:
        assert conn.execute('SELECT count(*) FROM listening_observations').fetchone()[0] == (2 if target >= 99 else 1)
    if target >= 31:
        assert list(map(tuple, conn.execute('SELECT id,video_id,played_at,is_generated FROM play_events ORDER BY id'))) == state['events']
        assert list(map(tuple, conn.execute('SELECT event_id,day FROM play_event_calendar ORDER BY event_id'))) == state['calendar']
        assert list(map(tuple, conn.execute('SELECT event_id,source FROM play_event_sources ORDER BY event_id'))) == state['sources']
    if 50 <= target <= 79:
        source = conn.execute("SELECT payload FROM rec_musical_sources WHERE provider='fixture'").fetchone()[0]
        assert json.loads(source)['tags']['value'] == []
        assert 'delivery_id' not in source and 'input_title' not in source
        assert conn.execute("SELECT count(*) FROM rec_musical_links l JOIN rec_musical_sources s "
            "ON s.source_key=l.source_key WHERE s.provider='fixture'").fetchone()[0] == 1
        if target < 79:
            assert list(map(tuple, conn.execute('SELECT * FROM metadata_findings ORDER BY id'))) == state['findings']
    if target >= 79:
        assert list(map(tuple, conn.execute('SELECT * FROM entry_artists WHERE entry_id=? ORDER BY position', (state['credits'],)))) == state['credit_rows']
        assert list(map(tuple, conn.execute('SELECT * FROM entry_artist_selections WHERE entry_id=?', (state['credits'],)))) == state['credit_selections']
        # v099 also converts the two captured album denominators into real
        # occurrence evidence. The credit rename itself never invalidates it.
        album_changes = 4 if target >= 99 else 0
        assert conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] == state['credit_changes'] + album_changes
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='track_artists' OR sql LIKE '%track_artists%'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert not (state['path'].parent / 'backups').exists()


def assert_declared_contract(conn, target, empty_reference):
    expected = dict(empty_reference['expected'])
    if target < 8:
        # The literal pre-enrichment tracks table predates v001's compound
        # uniqueness declaration; the v008 replacement supplies its own keys.
        kind, declaration = expected['tracks']
        assert len(declaration[2]) == 1
        key = next(iter(declaration[2]))
        assert key[0] == 'u' and tuple(column[0] for column in key[1]) == ('identity_key', 'video_id')
        expected['tracks'] = kind, (*declaration[:2], {}, *declaration[3:])
    if target < 32:
        # v001 adds the missing column to this literal predecessor. Its CHECK
        # is deliberately absent until the real v032 table replacement.
        kind, declaration = expected['play_events']
        assert len(declaration[3]) == 1
        assert 'is_generated' in next(iter(declaration[3]))
        expected['play_events'] = kind, (*declaration[:3], {}, *declaration[4:])
    assert objects(conn) == expected


def test_actual_registered_edge_keeps_original_rows_history_and_declared_contract(early, target, empty_reference):
    conn = early['conn']
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) if target > 1 else []
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    early['version'] = target
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:len(history)] == history
    seed_owner_inputs(early, target)
    assert_originals(early, target)
    assert_declared_contract(conn, target, empty_reference)


def rename_owner(state):
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(state['conn'], registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(state['conn'])
    state['programs'] = restore.call_args
    state['version'] = 103
    return state


def restore_programs(state):
    captured = state['programs']
    with state['conn']:
        state['conn'].execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(state['conn'])
    return state


@pytest.fixture(scope='module')
def empty_renamed(empty_reference):
    return rename_owner(empty_reference)


def test_actual_empty_rename_preserves_numbered_history(empty_renamed):
    assert empty_renamed['conn'].execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 103


@pytest.fixture(scope='module')
def empty_restored(empty_renamed):
    state = restore_programs(empty_renamed)
    state['expected'] = objects(state['conn'])
    return state


def test_actual_empty_program_restoration_keeps_all_foreign_keys(empty_restored):
    runner.validate_version(empty_restored['conn'], 103, foreign_keys=False, data=False)
    assert not empty_restored['conn'].execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def renamed(early, empty_restored):
    return rename_owner(early)


def test_actual_account_rename_keeps_original_early_rows_and_play_evidence(renamed):
    assert_originals(renamed, 103)
    assert tuple(renamed['conn'].execute('SELECT id,label,credential_ref FROM accounts').fetchone()) == (3, 'main', 'credential')


@pytest.fixture(scope='module')
def restored(renamed):
    return restore_programs(renamed)


def test_real_rename_programs_restore_the_declared_contract(restored, empty_restored):
    assert_declared_contract(restored['conn'], 103, empty_restored)
    assert_originals(restored, 103)


@pytest.fixture(scope='module', params=range(104, len(registry.MIGRATIONS)+1))
def later_target(request):
    return request.param


def test_actual_empty_later_edge_establishes_its_independent_contract(empty_restored, later_target):
    runner.migrate(empty_restored['conn'], registry=registry.MIGRATIONS[:later_target], clock=lambda: 2.)
    empty_restored['expected'] = objects(empty_restored['conn'])
    assert empty_restored['conn'].execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == later_target
    assert not empty_restored['conn'].execute('PRAGMA foreign_key_check').fetchone()


def test_actual_later_edge_keeps_originals_and_exact_declared_contract(restored, empty_restored, later_target):
    target = later_target
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    restored['version'] = target
    assert_originals(restored, target)
    assert_declared_contract(restored['conn'], target, empty_restored)


@pytest.fixture(scope='module')
def regular_plan(restored):
    return regular_expectation(restored['conn'], clock=1234)


def test_regular_admission_oracle_captures_every_original_owner_without_writing(restored, regular_plan):
    assert len(regular_plan['rows']['schema_migrations']) == len(registry.MIGRATIONS)
    assert regular_plan['rows']['playlist_items'] == [(8, 'original-video', 0)]


def test_completed_empty_chain_replay_preserves_every_row_and_history(empty_restored):
    conn = empty_restored['conn']
    before = list(conn.iterdump())
    runner.migrate(conn, registry=registry.MIGRATIONS)
    assert list(conn.iterdump()) == before


@pytest.fixture(scope='module')
def empty_regular_plan(empty_restored):
    return regular_expectation(empty_restored['conn'], clock=1234)


def test_empty_regular_oracle_keeps_complete_numbered_history(empty_regular_plan):
    assert len(empty_regular_plan['rows']['schema_migrations']) == len(registry.MIGRATIONS)


@pytest.fixture(scope='module', params=range(1, len(regular.CHANGES)+1))
def regular_target(request):
    return request.param


def test_real_empty_regular_change_keeps_numbered_history(empty_restored, empty_regular_plan, regular_target):
    conn = empty_restored['conn']
    before = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    regular.migrate(conn, changes=regular.CHANGES[:regular_target], clock=lambda: 1234.)
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == before
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
        (c.version, c.name) for c in regular.CHANGES[:regular_target]]


def test_real_regular_change_preserves_populated_numbered_history(restored, regular_plan, regular_target):
    target = regular_target
    conn = restored['conn']
    before = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    regular.migrate(conn, changes=regular.CHANGES[:target], clock=lambda: 1234.)
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == before
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
        (c.version, c.name) for c in regular.CHANGES[:target]]
    assert_originals(restored, len(registry.MIGRATIONS))


def test_completed_regular_admission_matches_independent_every_row_and_program_oracle(restored, regular_plan):
    assert_regular_expectation(restored['conn'], regular_plan)
    current.validate(restored['conn'])


def test_empty_regular_admission_matches_every_original_row_and_program(empty_restored, empty_regular_plan):
    assert_regular_expectation(empty_restored['conn'], empty_regular_plan)


@pytest.fixture(scope='module')
def fresh_current():
    with closing(sqlite3.connect(':memory:')) as fresh:
        runner.migrate(fresh)
        fresh.execute('PRAGMA query_only=ON')
        yield fresh


def test_fresh_current_oracle_has_complete_histories_and_foreign_keys(fresh_current):
    assert fresh_current.execute('PRAGMA query_only').fetchone()[0] == 1
    assert fresh_current.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == len(registry.MIGRATIONS)
    assert regular.validate(fresh_current) == len(regular.CHANGES)
    assert not fresh_current.execute('PRAGMA foreign_key_check').fetchone()


def test_early_upgraded_schema_matches_fresh_current_objects(restored, fresh_current):
    assert objects(restored['conn'], canonical_versions=True) == objects(fresh_current, canonical_versions=True)


def test_empty_full_chain_matches_fresh_objects_and_every_seed_row(empty_restored, fresh_current):
    conn = empty_restored['conn']
    assert objects(conn, canonical_versions=True) == objects(fresh_current, canonical_versions=True)
    for table, (kind, _) in objects(conn).items():
        if kind == 'table':
            columns = 'version,name' if table in ('schema_migrations', 'schema_changes') else '*'
            assert sorted(map(tuple, conn.execute('SELECT '+columns+' FROM "'+table+'"')), key=repr) == sorted(
                map(tuple, fresh_current.execute('SELECT '+columns+' FROM "'+table+'"')), key=repr), table
    before = list(conn.iterdump())
    runner.migrate(conn)
    assert list(conn.iterdump()) == before


def test_early_store_binds_real_repositories_and_keeps_unknown_and_frozen_event_evidence(restored):
    store = restored['store']
    store.init_schema()
    assert isinstance(store.taste_groups, CompactTasteGroups)
    assert store.taste_groups is not restored['old_groups']
    assert store._paid_descriptions and store._recommendation_files
    assert store.settings.get_setting('user-setting') == 'keep me'
    assert float(store.settings.get_setting('last_sync_at')) == 1000
    settings = dict(store.conn.execute('SELECT key,value FROM settings'))
    assert 'rec_param:onboard_coverage_min' not in settings
    assert 'rec_param:onboard_library_min' not in settings
    assert settings['rec_param:onboard_feedback_min'] == '15'
    assert settings['onboard_dismissed'] == '1'
    assert tuple(store.conn.execute('SELECT id,orig_title,orig_artist FROM tracks WHERE id=17').fetchone()) == (17, 'Original title', 'Artist')
    assert tuple(store.conn.execute('SELECT id,account_id,video_id,played_at,is_generated FROM play_events WHERE id=9').fetchone()) == (
        9, 3, 'original-video', 100, None)
    assert list(map(tuple, store.conn.execute('SELECT p.id,p.raw_identity_key,p.video_id,p.played_at,s.source,p.is_generated '
        'FROM play_events p JOIN play_event_sources s ON s.event_id=p.id WHERE p.id<=34 ORDER BY p.id'))) == restored['raw_events']
    assert list(map(tuple, store.conn.execute('SELECT identity_key,recording_id FROM play_event_subjects WHERE id>=30 AND id<=34 ORDER BY id'))) == [
        ('song|artist', 1)] * 4 + [('@unresolved-video:unknown', None)]
    assert store.rec_model.weight_records()['genre:house'] == {'axis': 'genre:house', 'weight': 1.4, 'updated_at': 1.}
    assert not store.rec_model.get_rec_vectors()
    fields = metadata_provenance(store, 200)['fields']
    assert fields['genre'] == {'value': 'Rock', 'override': False, 'source': 'legacy_unknown'}
    assert fields['title'] == {'value': 'Edited', 'override': True, 'source': 'user'}
    assert fields['orig_title']['value'] == 'Original'
    restored['provenance'] = metadata_provenance(store, 200)
    assert [row['credit'] for row in store.artists.track_credits(restored['credits'])] == ['First', 'Second']
    assert store.conn.execute('SELECT track_keys FROM album_tracklist_keys').fetchone()[0] == '["first|artist","second|artist"]'
    assert store.conn.execute('SELECT count(*) FROM album_share_snapshots').fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM album_tracklists').fetchone()[0] == 1


def test_repeated_current_admission_keeps_repository_identity_and_every_sql_row(restored):
    store = restored['store']
    repo = store.tracks
    before = list(store.conn.iterdump())
    store.init_schema()
    assert store.tracks is repo
    assert list(store.conn.iterdump()) == before
    assert metadata_provenance(store, 200) == restored['provenance']
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_both_actual_memory_readers_bind_the_upgraded_compact_owners(restored):
    memory = Store(':memory:')
    with closing(memory.conn):
        restored['conn'].backup(memory.conn)
        memory.init_schema()
        assert isinstance(memory.taste_groups, CompactTasteGroups)
        with memory.read_view() as reader:
            assert isinstance(reader.taste_groups, CompactTasteGroups)
            assert reader._paid_descriptions and reader._recommendation_files
            assert reader.taste_groups.head() is None
        with memory.read_snapshot() as snapshot:
            assert isinstance(snapshot.store.taste_groups, CompactTasteGroups)
            assert snapshot.store._paid_descriptions and snapshot.store._recommendation_files
            assert snapshot.store.taste_groups.head() is None
        repo = memory.taste_groups
        memory.init_schema()
        assert memory.taste_groups is repo


def test_real_reopen_preserves_all_early_rows_and_repaired_generated_policy(restored):
    before = list(restored['conn'].iterdump())
    store = Store(restored['path'])
    with closing(store.conn):
        store.init_schema()
        assert list(store.conn.iterdump()) == before
        assert metadata_provenance(store, 200) == restored['provenance']
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            store.conn.execute('UPDATE play_events SET is_generated=2')
        store.conn.rollback()
        assert list(store.conn.iterdump()) == before
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_legacy_genre_audio_and_ranker_columns_are_available_through_current_readers(restored):
    store = restored['store']
    assert any(row['artist'] == 'Old Artist' for row in store.discovery.get_discovered_artists())
    assert all('genre' in row for row in store.discovery.get_discovered_artists())
    columns = {row['name'] for row in store.conn.execute('PRAGMA table_info(tracks)')}
    assert {'bpm', 'energy', 'danceability', 'mb_recording_id', 'music_key', 'music_scale',
        'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental', 'loudness',
        'dynamic_complexity', 'popularity', 'gain', 'label'} <= columns
    for table in ('rec_mode_impressions', 'rec_mode_picks'):
        assert 'ranker' in {row['name'] for row in store.conn.execute('PRAGMA table_info('+table+')')}


def test_current_audio_samples_labels_and_original_measurement_survive_every_owner(restored):
    store, value = restored['store'], restored['sample']
    assert store.audio_vibes.samples(value['run_id']) == [value]
    assert store.audio_vibes.earlier_feedback(value['run_id']) == [{
        'taxonomy_version': 'retained-taxonomy', 'vibes': ['relax'],
        'sample_ids': [value['id']], 'prediction': None, 'updated_at': 11.}]
    score = store.conn.execute('SELECT id,score,model_version,measured_at FROM measurements').fetchone()
    assert tuple(score)[1:] == (.8, 'old-model', None)
    assert store.conn.execute('SELECT measurement_id FROM recording_vibes').fetchone()[0] == score[0]
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN "
        "('audio_vibe_samples','rec_vibe_tracks','rec_vibe_groups')").fetchone()


def test_current_metadata_cutover_preserves_the_whole_catalog_row_and_choice_delivery(restored):
    store = restored['store']
    entry, key, before = restored['cutover']
    assert dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
    assert store.tracks.identity_key_for_video('video') == key
    assert metadata_provenance(store, entry)['lineage']['genre']['choice']['delivery']


def test_current_listening_subjects_preserve_every_original_field_source_and_calendar(restored):
    store = restored['store']
    for table, rows in restored['listening'].items():
        relation = {'play_events': 'play_event_subjects', 'listening_observations': 'listening_observation_subjects'}.get(table, table)
        converted = [dict(row) for row in store.conn.execute('SELECT * FROM '+relation+' ORDER BY '+('id' if table in (
            'play_events', 'listening_observations') else 'event_id'))]
        expected = [{('account_id' if key == 'identity_id' else key): value for key, value in row.items()
            if key != 'source' or table != 'play_events'} for row in rows]
        assert len(converted) == len(expected)
        assert [{key: row[key] for key in original} for row, original in zip(converted, expected)] == expected


def test_real_requeued_discovery_preserves_metadata_and_accepts_a_new_attempt(restored):
    from tests.enrichment_support import complete_discovered

    store = restored['store']
    # Original, credited, provider-cutover, four duplicate subjects and the
    # original discovery remain eligible. The title-only subject has no artist.
    assert store.enrichment_reports.queue_remaining() == 8
    assert 17 in {row['id'] for row in store.enrichment_queue.next_enrich_batch(10)}
    row = store.discovery.discovered_tracks_by_keys(['cold'])['cold']
    assert row['last_enriched'] is None
    assert (row['genre'], row['year'], row['audio']) == ('House', '2020', {'bpm': 128.0})
    store.enrichment_state.record_enrichment_attempt('cold', 'deezer', {}, now=300)
    complete_discovered(store, ['cold'], 300)


def test_fresh_reopen_keeps_new_attempt_and_does_not_requeue_completed_discovery_again(restored):
    store = Store(restored['path'])
    with closing(store.conn):
        store.init_schema()
        assert store.enrichment_reports.queue_remaining() == 7
        duplicate_entries = {store.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()[0]
            for video, _alias in restored['duplicates'].values()}
        assert {row['id'] for row in store.enrichment_queue.next_enrich_batch(10)} == {
            17, restored['credits'], restored['cutover'][0], *duplicate_entries}
        assert store.enrichment_state.enrichment_attempts('cold')['deezer']['status'] == 'no_match'
        value = restored['sample']
        assert store.audio_vibes.samples(value['run_id']) == [value]
        entry, key, before = restored['cutover']
        assert dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
        assert store.tracks.identity_key_for_video('video') == key
        assert metadata_provenance(store, entry)['lineage']['genre']['choice']['delivery']
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_upgraded_credit_write_invalidates_preparation_without_changing_order(restored):
    store = restored['store']
    with store.conn:
        store.conn.execute("UPDATE entry_artists SET credit='New credit' WHERE entry_id=? AND position=0", (restored['credits'],))
    assert store.conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] > restored['credit_changes']
    assert [row['credit'] for row in store.artists.track_credits(restored['credits'])] == ['New credit', 'Second']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('values', [('Rock', 'Pop'), ('Rock', '')])
@pytest.mark.parametrize('reverse', [False, True])
def test_distinct_duplicate_decisions_keep_order_and_require_explicit_resolution(restored, values, reverse):
    store = restored['store']
    video, alias = restored['duplicates'][reverse, values]
    entry = store.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()[0]
    assert store.tracks.get_track_enrichment(entry)[0] == ''
    detail = store.enrichment_reports.enrichment_conflict_detail(entry, 'genre')
    assert {option['value'] for option in detail['options']} >= set(values)
    assert tuple(store.conn.execute('SELECT resolved,resolved_value FROM enrichment_conflict WHERE track_id=?',
        (entry,)).fetchone()) == (0, None)
    assert store.conn.execute('SELECT count(*) FROM entry_field_choices WHERE entry_id=?', (entry,)).fetchone()[0] == 0
    choices = store.conn.execute('SELECT value_json,chosen_at FROM metadata_decisions '
        'WHERE entry_id=? AND legacy_ref IS NOT NULL ORDER BY chosen_at', (entry,)).fetchall()
    assert [(json.loads(row[0]), row[1]) for row in choices] == [(values[0], 10), (values[1], 20)]
    before = metadata_provenance(store, entry)
    store.init_schema()
    assert metadata_provenance(store, entry) == before
    store.enrichment.resolve_conflict(alias, 'genre', values[1], now=30)
    assert store.tracks.get_track_enrichment(entry)[0] == values[1]
    assert tuple(store.conn.execute('SELECT resolved,resolved_value FROM enrichment_conflict WHERE track_id=?',
        (entry,)).fetchone()) == (1, values[1])
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_validated_upgraded_catalog_allows_verified_reassociation_after_both_identity_owners(restored):
    conn = restored['store'].conn
    with conn:
        first = catalog.upsert(conn, 'first', 'First', 'Artist')
        second = catalog.upsert(conn, 'second', 'Second', 'Artist')
    target = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (second,)).fetchone()[0]
    current.validate(conn)
    with conn:
        catalog.associate_item(conn, 'first', target, source='verified', evidence={'review': 'same recording'}, now=1)
    assert conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (first,)).fetchone()[0] == target
    assert conn.execute("SELECT recording_id FROM youtube_items WHERE video_id='first'").fetchone()[0] == target
    current.validate(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_explicit_future_owner_failure_rolls_back_every_upgraded_row_and_history(restored):
    conn = restored['conn']
    before = list(conn.iterdump())

    def interrupted(connection, context):
        connection.execute('CREATE TABLE later (id INTEGER PRIMARY KEY)')
        raise ValueError('injected later failure')

    extended = (*registry.MIGRATIONS, Migration(current.VERSION + 1, 'later', interrupted))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, 'MIGRATIONS', extended)
        with pytest.raises(MigrationError, match='injected later failure'):
            runner.migrate(conn, registry=extended)
    assert list(conn.iterdump()) == before


def test_explicit_future_owner_retry_commits_once_on_the_populated_upgraded_catalog(restored):
    conn = restored['conn']
    extended = (*registry.MIGRATIONS, Migration(current.VERSION + 1, 'later', lambda c, x:
        c.execute('CREATE TABLE later (id INTEGER PRIMARY KEY)')))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, 'MIGRATIONS', extended)
        runner.migrate(conn, registry=extended)
        before = list(conn.iterdump())
        runner.migrate(conn, registry=extended)
        assert list(conn.iterdump()) == before
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION + 1
    assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='later'").fetchone()
