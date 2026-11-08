"""Populated capture conversion retains uncertain evidence and recovery originals."""
from contextlib import ExitStack, closing
import json
import sqlite3

import pytest

from tests.historical_captures import legacy_capture
from tests.core.test_early_schema_upgrade import rename_owner, restore_programs
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template, cleanup_memory_files, roots
from yt_playlist.core import catalog, repair
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current, regular
from yt_playlist.core.migrations import v033_capture_evidence as capture
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def _predecessor(stack, path=':memory:'):
    store = Store(path)
    stack.callback(store.conn.close)
    migrate(store.conn, registry=MIGRATIONS[:32])
    seed_capture_predecessor(store)
    return store


def seed_capture_predecessor(store):
    with store.conn:
        store.conn.executemany('INSERT INTO identities(id,label,credential_ref,is_master) VALUES (?,?,?,?)',
                               [(1, 'Main', 'main', 1), (2, 'Other', 'other', 0)])
        store.conn.execute("INSERT INTO listening_calendars VALUES (1,'America/Costa_Rica',1)")
        store.conn.execute("INSERT INTO listening_calendars VALUES (2,'UTC',0)")
        store.conn.execute(
            'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,source_day,day,'
            'source,timezone,precision,first_observed_at,last_observed_at) '
            "VALUES (1,'raw day','day','2026-10-20','2026-10-20','independent',"
            "'America/Costa_Rica','calendar_day',1,2)")
    legacy_capture(store.conn, 1, 1792540800, ['unknown', 'unknown'], source='unknown')
    legacy_capture(store.conn, 1, 1792540801, ['ytm'], source='ytm_history')
    legacy_capture(store.conn, 2, 1792540802, ['unknown'], source='independent')
    legacy_capture(store.conn, 2, 1792540803, [])


@pytest.fixture
def predecessor():
    with ExitStack() as stack:
        yield lambda path=':memory:': _predecessor(stack, path)


def seed_converted_capture(source):
    seed_capture_predecessor(source)
    # Keep both accounts' same-source captures and the resolved recording used
    # by the independent reassociation owner below.
    legacy_capture(source.conn, 2, 1792540801, ['ytm'], source='ytm_history')
    with source.conn:
        source.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Correction','third',0)")
        source.conn.execute("INSERT INTO listening_calendars VALUES (3,'UTC',0)")
        entries = []
        for video, title in [('old', 'Old'), ('correct', 'Correct')]:
            key = f'{video}|artist'
            recording = source.conn.execute(
                'INSERT INTO recordings(identity_key,title,artist) VALUES (?,?,?)',
                (key, title, 'Artist')).lastrowid
            source.conn.execute(
                'INSERT INTO youtube_items(video_id,recording_id,title,artist,duration_s) VALUES (?,?,?,?,200)',
                (video, recording, title, 'Artist'))
            entries.append(source.conn.execute(
                'INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)',
                (recording, video)).lastrowid)
            source.conn.execute('INSERT INTO recording_aliases VALUES (?,?,?)', (key, recording, 'fixture'))
    receipt_id = legacy_capture(source.conn, 3, 1792540800, ['old|artist'], source='unknown')
    return {
        'originals': _originals(source.conn),
        'days': list(map(tuple, source.conn.execute('SELECT * FROM listening_days ORDER BY identity_id,identity_key'))),
        'observations': [dict(r) for r in source.conn.execute('SELECT * FROM listening_observations')],
        'receipts': list(map(tuple, source.conn.execute('SELECT * FROM history_snapshots ORDER BY id'))),
        'entries': entries, 'receipt_id': receipt_id,
    }


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These historical SQL owners do not start an HTTP app."""


@pytest.fixture(scope='module', autouse=True)
def isolated_capture_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('capture-upgrade-home')))
        bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz')))
        yield


@pytest.fixture(scope='module')
def capture_journey(tmp_path_factory, _pristine_installed_schema):
    path = tmp_path_factory.mktemp('like-capture-upgrade') / 'installed.db'
    source = Store(path)
    with closing(source.conn):
        _pristine_installed_schema(6).backup(source.conn)
        source.conn.execute('PRAGMA journal_mode=WAL')
        source.conn.execute('PRAGMA synchronous=NORMAL')
        source._bind_repositories()
        yield dict(source=source, conn=source.conn, path=path, version=6)


def assert_journey_history(state):
    assert list(map(tuple, state['conn'].execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (migration.version, migration.name) for migration in MIGRATIONS[:state['version']]]
    assert not state['conn'].execute('PRAGMA foreign_key_check').fetchone()


def test_frozen_like_input_keeps_the_genuine_six_version_predecessor(capture_journey):
    assert_journey_history(capture_journey)
    assert not capture_journey['conn'].execute('SELECT 1 FROM tracks').fetchone()


def test_actual_frozen_like_repair_keeps_earned_crossings_and_once_ever_stamp(capture_journey):
    store, now = capture_journey['source'], 1_000_000
    with store.conn:
        for number in range(3):
            key = f'song{number}|artist'
            store.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key,genre,mb_year) "
                "VALUES (?,?,'Artist',?,'House','1993-04-05')", (str(number), f'Song{number}', key))
            store.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('like',?,'like',?)",
                               (key, now-100+number))
        for axis in ('genre:house', 'era:1990', 'artist:Artist'):
            store.conn.execute('INSERT INTO rec_weights VALUES (?,2,?)', (axis, now))
            store.conn.executemany("INSERT INTO rec_grad_log(axis,source,score,factor,new_weight,created_at) "
                "VALUES (?,'like',2,1.05,2,?)", [(axis, now-50+number) for number in range(14)])
    migrate(store.conn, registry=MIGRATIONS[:7], clock=lambda: now)
    capture_journey['version'] = 7
    for axis in ('genre:house', 'era:1990', 'artist:Artist'):
        assert store.rec_model.get_weights(now=now)[axis] == pytest.approx(2 / 1.05 ** 12)
    stamp = store.settings.get_setting('like_ratchet_repaired')
    assert '3 once-ever graduation stamp(s)' in stamp
    capture_journey['like'] = dict(stamp=stamp, weights=store.rec_model.weight_records())
    assert_journey_history(capture_journey)


@pytest.mark.parametrize('version', range(8, 33))
def test_actual_early_upgrade_keeps_the_frozen_like_entitlement(capture_journey, version):
    migrate(capture_journey['conn'], registry=MIGRATIONS[:version])
    capture_journey['version'] = version
    assert_journey_history(capture_journey)
    assert capture_journey['conn'].execute("SELECT value FROM settings WHERE key='like_ratchet_repaired'").fetchone()[0] == capture_journey['like']['stamp']


def test_capture_predecessor_keeps_exact_original_rows_days_and_receipts(capture_journey):
    original = seed_converted_capture(capture_journey['source'])
    capture_journey['original'] = original
    assert original['originals'] and len(original['receipts']) == 6
    assert_journey_history(capture_journey)


def test_actual_capture_conversion_preserves_every_raw_key_and_item_identity(capture_journey):
    migrate(capture_journey['conn'], registry=_registry())
    capture_journey['version'] = 33
    capture.validate(capture_journey['conn'])
    assert list(map(tuple, capture_journey['conn'].execute(
        'SELECT identity_id,raw_identity_key,identity_key,recording_id,source,last_observed_at,'
        'capture_id,capture_item_id FROM listening_observations WHERE capture_id IS NOT NULL '
        'ORDER BY capture_item_id'))) == capture_journey['original']['originals']
    assert_journey_history(capture_journey)


@pytest.mark.parametrize('version', range(34, 103))
def test_actual_later_capture_upgrade_keeps_receipts_and_original_precision(capture_journey, version):
    migrate(capture_journey['conn'], registry=MIGRATIONS[:version])
    capture_journey['version'] = version
    assert_journey_history(capture_journey)
    assert capture_journey['conn'].execute('SELECT count(*) FROM listening_capture_receipts').fetchone()[0] == 6


def test_actual_capture_account_rename_keeps_both_earlier_contracts(capture_journey):
    rename_owner(capture_journey)
    assert_journey_history(capture_journey)


def test_actual_capture_program_restoration_preserves_the_declared_contract(capture_journey):
    restore_programs(capture_journey)
    assert_journey_history(capture_journey)


@pytest.mark.parametrize('version', range(104, len(MIGRATIONS)+1))
def test_actual_final_capture_upgrade_keeps_receipts_and_foreign_keys(capture_journey, version):
    migrate(capture_journey['conn'], registry=MIGRATIONS[:version])
    capture_journey['version'] = version
    assert_journey_history(capture_journey)


@pytest.mark.parametrize('version', range(1, len(regular.CHANGES)+1))
def test_each_actual_regular_owner_preserves_capture_receipts_and_both_histories(capture_journey, version):
    regular.migrate(capture_journey['conn'], changes=regular.CHANGES[:version])
    assert regular.validate(capture_journey['conn']) == version
    assert_journey_history(capture_journey)


@pytest.mark.parametrize('restart', [1, 2])
def test_fresh_store_admission_preserves_frozen_like_stamp_weights_and_captures(capture_journey, restart):
    source = Store(capture_journey['path'])
    with closing(source.conn):
        source.init_schema()
        assert source.settings.get_setting('like_ratchet_repaired') == capture_journey['like']['stamp']
        assert source.rec_model.weight_records() == capture_journey['like']['weights']
        assert source.conn.execute('SELECT count(*) FROM listening_capture_receipts').fetchone()[0] == 6
        assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def converted_capture_template(capture_journey):
    source = capture_journey['source']
    source.init_schema()
    cleanup_memory_files(source, roots(source.conn))
    current.validate(source.conn)
    return capture_template(source, capture_journey['original'])


def test_completed_capture_input_keeps_actual_snapshot_guards_and_current_history(converted_capture_template):
    source = converted_capture_template.source
    assert source._snapshot_revisions.conn is source.conn
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == len(MIGRATIONS)


@pytest.fixture
def converted_capture(converted_capture_template):
    with converted_capture_template.open(':memory:') as store:
        yield store


def _registry(apply=capture.apply):
    return (*MIGRATIONS[:32], Migration(33, 'capture_evidence', apply))


def _originals(conn):
    return list(map(tuple, conn.execute(
        'SELECT hs.identity_id,hi.raw_identity_key,hi.identity_key,hi.recording_id,hi.source,'
        'hs.taken_at,hs.id,hi.rowid FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id '
        'ORDER BY hi.rowid')))


def test_populated_conversion_preserves_originals_precision_receipts_and_projection(
        converted_capture, converted_capture_template):
    store = converted_capture
    original = converted_capture_template.result
    # Later migrations may replace these views while retaining the evidence.
    # Validate each schema against its own contract, then compare the data below.
    current.validate(store.conn)
    assert list(map(tuple, store.conn.execute(
        'SELECT account_id,raw_identity_key,identity_key,recording_id,source,last_observed_at,'
        "capture_id,capture_item_id FROM listening_observation_subjects WHERE precision='legacy_unknown' "
        'ORDER BY capture_item_id'))) == original['originals']
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_capture_receipts ORDER BY id'))) == original['receipts']
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_days ORDER BY account_id,identity_key'))) == original['days']
    converted = store.conn.execute('SELECT * FROM listening_observation_subjects WHERE id=1').fetchone()
    observation = original['observations'][0]
    assert {k:converted['account_id' if k == 'identity_id' else k] for k in observation} == observation
    assert list(map(tuple, store.conn.execute(
        "SELECT DISTINCT precision,timezone FROM listening_observations WHERE capture_id IS NOT NULL"))) == [
            ('legacy_unknown', None)]
    assert {r[0] for r in store.conn.execute(
        "SELECT precision FROM listening_activity WHERE identity_key='unknown'")} == {'legacy_unknown'}
    assert not list(store.conn.execute('SELECT 1 FROM play_events'))
    assert tuple(store.conn.execute('SELECT timezone,confirmed FROM listening_calendars WHERE account_id=1').fetchone()) == (
        'America/Costa_Rica', 1)
    for name in capture.RETIRED:
        assert store.conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None


def test_file_upgrade_preserves_evidence_and_reopens_without_backups(tmp_path, predecessor):
    store = predecessor(tmp_path / 'state.db')
    originals = _originals(store.conn)
    migrate(store.conn, registry=_registry())
    store.conn.close()
    assert not (tmp_path / 'backups').exists()
    with closing(sqlite3.connect(tmp_path / 'state.db')) as reopened, reopened:
        migrate(reopened, registry=_registry())
        capture.validate(reopened)
        assert reopened.execute('SELECT COUNT(*) FROM listening_capture_receipts').fetchone() == (4,)
        assert reopened.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert list(reopened.execute(
            'SELECT identity_id,raw_identity_key,identity_key,recording_id,source,last_observed_at,'
            'capture_id,capture_item_id FROM listening_observations WHERE capture_id IS NOT NULL '
            'ORDER BY capture_item_id')) == originals


def test_conversion_failure_restores_original_tables_rows_and_version(tmp_path, predecessor):
    store = predecessor(tmp_path / 'state.db')
    originals = _originals(store.conn)

    def interrupted(conn, context):
        capture.apply(conn, context)
        raise RuntimeError('injected interruption after retirement')

    with pytest.raises(MigrationError, match='injected interruption'):
        migrate(store.conn, registry=_registry(interrupted))
    assert _originals(store.conn) == originals
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 32
    assert store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='listening_capture_receipts'").fetchone() is None
    migrate(store.conn, registry=_registry())
    capture.validate(store.conn)


def test_first_confirmed_calendar_preserves_unknown_capture_dates_and_precision(converted_capture):
    from yt_playlist.core import listening

    store = converted_capture
    assert tuple(store.conn.execute('SELECT timezone,confirmed FROM listening_calendars WHERE account_id=3').fetchone())==('UTC',0)
    before = list(map(tuple, store.conn.execute(
        "SELECT * FROM listening_observations WHERE precision='legacy_unknown' ORDER BY id")))
    with store.conn:
        listening.calendar(store.conn, 3, 'America/Costa_Rica')
    assert list(map(tuple, store.conn.execute(
        "SELECT * FROM listening_observations WHERE precision='legacy_unknown' ORDER BY id"))) == before
    assert tuple(store.conn.execute('SELECT timezone,confirmed FROM listening_calendars WHERE account_id=3').fetchone())==('America/Costa_Rica',1)
    current.validate(store.conn)


def test_converted_capture_removal_and_reset_preserve_other_accounts_and_exact_plays(converted_capture):
    store = converted_capture
    other_rows = list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_observations WHERE account_id=2 ORDER BY id')))
    other_receipts = list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_capture_receipts WHERE account_id=2 ORDER BY id')))
    store.history.import_listening_events(1, [('real', None, 1792540800), ('real', None, 1792544400)])
    events = list(map(tuple, store.conn.execute('SELECT * FROM play_events ORDER BY id')))
    store.history.remove_listening_source('ytm_history', 1)
    assert not store.conn.execute(
        "SELECT 1 FROM listening_observations WHERE account_id=1 AND source='ytm_history'").fetchone()
    assert store.history.listening_day_counts(1) == {'day': 1, 'real': 1, 'unknown': 1}
    assert store.history.event_play_counts(1) == {'real': 2}
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_observations WHERE account_id=2 ORDER BY id'))) == other_rows
    store.trends.refresh_first_play()
    store.history.reset_play_history(1)
    assert not store.conn.execute('SELECT 1 FROM listening_observations WHERE account_id=1').fetchone()
    assert not store.conn.execute('SELECT 1 FROM listening_capture_receipts WHERE account_id=1').fetchone()
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_observations WHERE account_id=2 ORDER BY id'))) == other_rows
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_capture_receipts WHERE account_id=2 ORDER BY id'))) == other_receipts
    assert not store.trends.first_play_map('track')
    store.trends.refresh_first_play()
    assert store.settings.get_setting('trend_first_play_source') == 'canonical_listening'
    assert store.settings.get_setting('trend_first_play_watermark') is None
    store.history.reset_play_history()
    assert not store.conn.execute('SELECT 1 FROM listening_observations').fetchone()
    assert not store.conn.execute('SELECT 1 FROM listening_capture_receipts').fetchone()
    assert list(map(tuple, store.conn.execute('SELECT * FROM play_events ORDER BY id'))) == events
    current.validate(store.conn)


def test_converted_capture_reassociation_retains_completed_evidence_after_later_failure(
        monkeypatch, converted_capture, converted_capture_template):
    store = converted_capture
    first, target = converted_capture_template.result['entries']
    old_key, new_key = 'old|artist', 'correct|artist'
    receipt_id = converted_capture_template.result['receipt_id']
    original = dict(store.conn.execute(
        'SELECT * FROM listening_observations WHERE capture_id=?', (receipt_id,)).fetchone())
    invalidate = store.recording_keys.invalidate_catalog

    def interrupted():
        assert not store.conn.in_transaction
        invalidate()
        raise RuntimeError('interrupted reassociation')

    monkeypatch.setattr(store.recording_keys, 'invalidate_catalog', interrupted)
    recording_id = store.tracks.catalog_subject(target)['recording_id']
    with pytest.raises(RuntimeError, match='interrupted reassociation'):
        store.tracks.associate_youtube_item('old', recording_id, source='verified', evidence={'same': True}, now=1)
    assert not store.conn.in_transaction
    monkeypatch.setattr(store.recording_keys, 'invalidate_catalog', invalidate)
    store.tracks.associate_youtube_item('old', recording_id, source='verified', evidence={'same': True}, now=1)
    observed = dict(store.conn.execute(
        'SELECT * FROM listening_observations WHERE capture_id=?', (receipt_id,)).fetchone())
    assert observed == {**original, 'unresolved_subject_key': None, 'recording_id': recording_id}
    assert store.conn.execute('SELECT identity_key FROM listening_observation_subjects WHERE capture_id=?',
        (receipt_id,)).fetchone()[0] == new_key
    assert observed['raw_identity_key'] == old_key
    assert store.tracks.catalog_subject(first)['recording_id'] == recording_id
    current.validate(store.conn)


def test_interrupted_reset_rolls_back_observations_and_retains_receipts(converted_capture):
    store = converted_capture
    store.trends.refresh_first_play()
    with store.conn:
        store.conn.execute("CREATE TRIGGER interrupt_reset BEFORE DELETE ON listening_capture_receipts "
                           "BEGIN SELECT RAISE(ABORT,'interrupted reset'); END")
    before = list(store.conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='interrupted reset'):
        store.history.reset_play_history(1)
    assert not store.conn.in_transaction
    assert list(store.conn.iterdump()) == before


def test_migrated_repair_backup_restores_complete_capture_evidence(tmp_path, converted_capture):
    store = converted_capture
    day = 1792540800 // 86400
    with store.conn:
        store.conn.execute(
            'INSERT INTO takeout_imports(account_id,fingerprint,imported_at,unparsed_dates,'
            'account_verified,complete,coverage_start,coverage_end) VALUES (1,?,1,0,1,1,?,?)',
            ('verified coverage', day * 86400, (day + 1) * 86400))
    original = list(map(tuple, store.conn.execute('SELECT * FROM listening_observations ORDER BY id')))
    receipts = list(map(tuple, store.conn.execute('SELECT * FROM listening_capture_receipts ORDER BY id')))
    backup_path = tmp_path / 'recovery.jsonl'
    assert repair.purge_phantom_history(store.conn, backup_path) == 1
    deleted, = [json.loads(line) for line in backup_path.read_text().splitlines()]
    assert deleted['identity_key'] == 'ytm' and deleted['source'] == 'ytm_history'
    assert deleted['captured_at'] == 1792540801
    assert deleted['precision'] == 'legacy_unknown' and deleted['timezone'] is None
    assert store.history.listening_day_counts(1) == {'day': 1, 'unknown': 1}
    assert store.history.listening_day_counts(2) == {'unknown': 1, 'ytm': 1}
    # The receipt is retained even after its last observation is repaired away.
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_capture_receipts ORDER BY id'))) == receipts
    del deleted['captured_at']
    del deleted['identity_key']  # Read projection; the base owner stores only its unresolved subject.
    with store.conn:
        store.conn.execute('INSERT INTO listening_observations(' + ','.join(deleted) + ') VALUES ('
                           + ','.join('?' for _ in deleted) + ')', list(deleted.values()))
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_observations ORDER BY id'))) == original
    current.validate(store.conn)


@pytest.mark.parametrize('assignment', [pytest.param('account_id=2', id='identity_id=2'), 'first_observed_at=0', "day='2026-10-01'"])
def test_current_validation_rejects_capture_receipt_disagreement(assignment, converted_capture):
    store = converted_capture
    with store.conn:
        store.conn.execute('UPDATE listening_observations SET ' + assignment + ' WHERE capture_item_id=1')
    with pytest.raises(MigrationError, match='capture receipt'):
        current.validate(store.conn)


@pytest.mark.parametrize('precision,timezone,capture_id,item_id', [
    ('exact_timestamp', 'UTC', None, None),
    ('legacy_unknown', 'UTC', 1, 99),
    ('legacy_unknown', None, None, 99),
    ('calendar_day', None, None, None),
    ('calendar_day', 'UTC', 1, 99),
])
def test_unknown_captures_cannot_claim_exact_or_confirmed_calendar_precision(
        precision, timezone, capture_id, item_id, predecessor):
    store = predecessor()
    migrate(store.conn, registry=_registry())
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,source_day,day,source,'
            'timezone,precision,first_observed_at,last_observed_at,capture_id,capture_item_id) '
            "VALUES (1,'x','x','2026-10-20','2026-10-20','unknown',?,?,1,1,?,?)",
            (timezone, precision, capture_id, item_id))
