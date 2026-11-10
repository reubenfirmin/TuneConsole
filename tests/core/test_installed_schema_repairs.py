"""Installed layout variants retain their rows through real repair boundaries."""
from contextlib import closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from yt_playlist.core.migrations import Migration, MigrationError, registry, runner


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These migration owners never construct an application lifespan."""


@pytest.fixture(autouse=True)
def vocabulary(monkeypatch):
    bound_catalog(monkeypatch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz')))


def connect(path=':memory:'):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def populate(conn, version):
    with conn:
        conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'Main','bridge',1)")
        conn.execute("INSERT INTO recordings(id,identity_key,title,artist) VALUES (17,'song|artist','Song','Artist')")
        conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES ('song',17,'Song','Artist')")
        conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (17,17,'song')")
        conn.execute("INSERT INTO library_memberships VALUES (17,'fixture',NULL)")
        conn.execute("INSERT INTO settings VALUES ('personal-setting','retain')")
        if version < 33:
            conn.execute('INSERT INTO history_snapshots VALUES (1,1,100)')
            conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'song|artist')")
        else:
            from yt_playlist.core import listening
            listening.observe_day(conn, 1, 'song|artist', '1970-01-01', 'independent', 'UTC', 100)


def assert_repaired(conn, version, before_history):
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    assert history[:-1] == before_history
    assert history[-1][:2] == (version, registry.MIGRATIONS[version - 1].name)
    runner.validate_version(conn, version)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    before = list(conn.iterdump())
    runner.migrate(conn, registry=registry.MIGRATIONS[:version])
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('missing_lookup', [False, True])
def test_installed_v64_repair_preserves_catalog_and_exact_lookup(missing_lookup):
    from yt_playlist.core.migrations import v065_group_library_lookup

    with closing(connect()) as conn:
        runner.migrate(conn, registry=registry.MIGRATIONS[:64])
        populate(conn, 64)
        if missing_lookup:
            with conn:
                conn.execute('DROP INDEX ix_group_library_inputs')
        before = list(map(tuple, conn.execute('SELECT * FROM recordings')))
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        runner.migrate(conn, registry=registry.MIGRATIONS[:65])
        assert list(map(tuple, conn.execute('SELECT * FROM recordings'))) == before
        v065_group_library_lookup.validate(conn)
        assert_repaired(conn, 65, history)


@pytest.fixture
def policy_database(tmp_path):
    with closing(connect(tmp_path / 'policy.db')) as conn:
        # An independent pre-v001 declaration reproduces its omitted CHECK.
        # The actual baseline adds the optional column; no migration receipt
        # is forged onto a modern replacement table.
        conn.execute('CREATE TABLE play_events (id INTEGER PRIMARY KEY,'
            'identity_id INTEGER NOT NULL REFERENCES identities(id),identity_key TEXT NOT NULL,'
            'video_id TEXT,played_at REAL NOT NULL,playlist_ytm_id TEXT,like_status TEXT)')
        runner.migrate(conn, registry=registry.MIGRATIONS[:31])
        populate(conn, 31)
        with conn:
            conn.executemany('INSERT INTO play_events(identity_id,identity_key,video_id,played_at,'
                "is_generated,source) VALUES (1,'song|artist','song',?,?,'live')",
                [(1, None), (2, 0), (3, 1)])
            conn.execute("INSERT INTO spotify_history_listens VALUES (1,'spotify:track:one',1,1,NULL)")
        yield conn


def policy_rows(conn):
    fields = ('id,identity_id,identity_key,video_id,played_at,playlist_ytm_id,'
              'like_status,source,is_generated,recording_id,raw_identity_key')
    return {'events': list(map(tuple, conn.execute('SELECT '+fields+' FROM play_events ORDER BY id'))),
            **{table: list(map(tuple, conn.execute('SELECT * FROM '+table))) for table in (
                'play_event_calendar', 'play_event_sources', 'spotify_history_listens')}}


def test_generated_policy_repair_keeps_all_event_and_dependent_evidence(policy_database):
    conn = policy_database
    before = policy_rows(conn)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    runner.migrate(conn, registry=registry.MIGRATIONS[:32])
    assert policy_rows(conn) == before
    with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
        conn.execute('UPDATE play_events SET is_generated=2')
    conn.rollback()
    assert_repaired(conn, 32, history)


def test_interrupted_generated_policy_repair_rolls_back_then_retries(policy_database, tmp_path):
    conn = policy_database
    before = list(conn.iterdump())
    rows = policy_rows(conn)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    repair = registry.MIGRATIONS[31]

    def interrupted(connection, context):
        repair.apply(connection, context)
        raise RuntimeError('interrupted after policy replacement')

    with pytest.raises(MigrationError, match='interrupted after policy replacement'):
        runner.migrate(conn, registry=(*registry.MIGRATIONS[:31],
            Migration(32, repair.name, interrupted)))
    assert list(conn.iterdump()) == before
    assert not (tmp_path / 'backups').exists()
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    runner.migrate(conn, registry=registry.MIGRATIONS[:32])
    assert policy_rows(conn) == rows
    assert_repaired(conn, 32, history)


@pytest.mark.parametrize('has_counts_view', [False, True])
def test_installed_v37_repair_preserves_observations_and_day_counts(has_counts_view, monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    with closing(connect()) as conn:
        runner.migrate(conn, registry=registry.MIGRATIONS[:37])
        populate(conn, 37)
        if has_counts_view:
            with conn:
                conn.execute('CREATE VIEW listening_day_counts AS SELECT identity_key,COUNT(*) plays '
                    'FROM (SELECT DISTINCT identity_id,identity_key,day FROM eligible_listening_evidence) '
                    'GROUP BY identity_key')
        before = list(map(tuple, conn.execute('SELECT * FROM listening_observations')))
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        runner.migrate(conn, registry=registry.MIGRATIONS[:38])
        assert list(map(tuple, conn.execute('SELECT * FROM listening_observations'))) == before
        assert tuple(conn.execute('SELECT * FROM listening_day_counts').fetchone()) == ('song|artist', 1)
        assert_repaired(conn, 38, history)
