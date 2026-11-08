"""Account changes cannot strand history behind an already advanced first-play checkpoint."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from tests.historical_captures import retained_capture
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations import v033_capture_evidence
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store

DAY = 86400


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/index checks do not construct the web application."""


def _history(store, key, day):
    return store.history.record_history_plays(1, day * DAY, [key], source_timezone="UTC")


def _assert_first(store, key, artist, day):
    store.trends.refresh_first_play()
    assert store.trends.first_play_map("track")[key] == day
    by_name = {store.trends.artist_labels()[aid]:first for aid,first in store.trends.first_play_map('artist').items()}
    assert by_name[artist] == day
    assert store.settings.get_setting('trend_first_play_watermark') is None
    assert store.settings.get_setting('trend_first_play_source') == 'canonical_listening'


@pytest.mark.parametrize("old_version", [7, 15])
def test_catalog_checkpoint_upgrade_invalidates_index_and_retains_history(old_version):
    # Exercise the real historical catalog/checkpoint cutover. Later capture
    # conversion and the current index/reopen contract have explicit owners below.
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn, registry=MIGRATIONS[:old_version])
        _seed_checkpoint(conn, old_version)
        before = list(conn.execute('SELECT * FROM history_snapshots ORDER BY id'))
        migrate(conn, registry=MIGRATIONS[:16])
        assert list(conn.execute('SELECT * FROM history_snapshots ORDER BY id')) == before
        assert list(conn.execute('SELECT snapshot_id,identity_key FROM history_items ORDER BY snapshot_id')) == [
            (1, 'song|artist'), (2, 'song|artist')]
        assert not conn.execute('SELECT * FROM trend_first_play').fetchall()
        assert not conn.execute("SELECT * FROM settings WHERE key LIKE 'trend_first_play_%'").fetchall()
        assert conn.execute("SELECT r.identity_key FROM youtube_items y JOIN recordings r "
                            "ON r.id=y.recording_id WHERE y.video_id='v'").fetchone() == ('song|artist',)
        # Keep the intervening artist/catalog/listening conversions in this
        # path: a native v32 fixture alone cannot establish their preservation.
        conn.row_factory = sqlite3.Row
        migrate(conn, registry=MIGRATIONS[:33])
        v033_capture_evidence.validate(conn)
        assert list(map(tuple, conn.execute('SELECT raw_identity_key,identity_key,recording_id,source_day,precision,timezone '
            'FROM listening_observations ORDER BY capture_item_id'))) == [
                ('song|artist', 'song|artist', 1, '1970-01-11', 'legacy_unknown', None),
                ('song|artist', 'song|artist', 1, '1970-01-21', 'legacy_unknown', None)]
        assert tuple(conn.execute('SELECT a.name FROM track_artists c JOIN artists a ON a.id=c.artist_id '
                                 'WHERE c.entry_id=1').fetchone()) == ('Artist',)
        assert not conn.execute('SELECT * FROM play_events').fetchall()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def _seed_checkpoint(conn, old_version):
    conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
    if old_version < 8:
        conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key) VALUES ('v','Song','Artist','song|artist')")
    else:
        # Seed version 15 using its own schema. Today's repositories require metadata
        # provenance tables introduced later, so they cannot construct this fixture.
        conn.execute("INSERT INTO recordings(id,identity_key,title,artist) VALUES (1,'song|artist','Song','Artist')")
        conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist,duration_s,available) VALUES ('v',1,'Song','Artist',200,1)")
        conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v')")
        conn.execute("INSERT INTO library_memberships(entry_id,origin) VALUES (1,'legacy_library')")
        conn.execute("INSERT INTO recording_aliases(alias,recording_id,source) VALUES ('song|artist',1,'legacy_library')")
    conn.execute("INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (1,1,?)", (10 * DAY,))
    conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'song|artist')")
    conn.execute("INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (2,1,?)", (20 * DAY,))
    conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (2,'song|artist')")
    conn.execute("INSERT INTO trend_first_play VALUES ('track','song|artist',10,?,'history')", (10 * DAY,))
    conn.execute("INSERT INTO trend_first_play VALUES ('artist','Artist',10,?,'history')", (10 * DAY,))
    conn.executemany('INSERT INTO settings VALUES (?,?)', [
        ('trend_first_play_watermark', '2'), ('trend_first_play_source', 'history')])
    conn.commit()


def test_history_capture_cutover_preserves_first_dates_and_reopens(tmp_path):
    path = tmp_path / 'capture.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        # This is the shipped frozen predecessor, rather than reconstructing
        # it through today's assembled definition or unrelated old upgrades.
        definition = Path(v033_capture_evidence.__file__).with_name('schema_v032.sql').read_text()
        conn.executescript('BEGIN;\n' + definition + '\nCOMMIT;')
        with conn:
            conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                             [(m.version, m.name) for m in MIGRATIONS[:32]])
        _seed_checkpoint(conn, 32)
        migrate(conn, registry=MIGRATIONS[:33])
        v033_capture_evidence.validate(conn)
    with closing(sqlite3.connect(path)) as reopened:
        migrate(reopened, registry=MIGRATIONS[:33])
        v033_capture_evidence.validate(reopened)
        assert list(reopened.execute('SELECT identity_id,captured_at FROM listening_capture_receipts ORDER BY id')) == [
            (1, 10 * DAY), (1, 20 * DAY)]
        assert list(reopened.execute('SELECT identity_key,source_day,precision,timezone FROM '
            'listening_observations ORDER BY capture_item_id')) == [
                ('song|artist', '1970-01-11', 'legacy_unknown', None),
                ('song|artist', '1970-01-21', 'legacy_unknown', None)]
        assert not reopened.execute('SELECT * FROM play_events').fetchall()
        assert not reopened.execute('SELECT * FROM trend_first_play').fetchall()
        assert not reopened.execute("SELECT * FROM settings WHERE key LIKE 'trend_first_play_%'").fetchall()


@pytest.fixture(scope='module')
def index_schema():
    # Initial index construction has no snapshot/concurrent-publication claim.
    # Reopening below runs real admission and installs real revision guards.
    with bounded_consumer_schema((('rock', 'Rock'),), snapshot_guards=False) as schema:
        yield schema


def test_converted_history_rebuilds_current_index_after_file_reopen(tmp_path, index_schema):
    path = tmp_path / 'index.db'
    store = Store(path)
    with closing(store.conn):
        # This owner checks committed persistence across an ordinary reopen,
        # not power-loss durability or background checkpoint scheduling.
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        initialize_consumer(store, index_schema, snapshot_guards=False)
        account = store.accounts.upsert_account('main', 'bridge', None, True)
        seed_publication_subjects(store, [{'key': 'song|artist', 'title': 'Song', 'artist': 'Artist', 'video_id': 'v'}])
        with store.conn:
            store.conn.execute("INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',0)")
            store.conn.execute("INSERT INTO entry_artists(entry_id,position,artist_id,credit,source) "
                               "VALUES (1,0,1,'Artist','fixture')")
        # Already-converted input belongs below this current index consumer.
        # The preceding test owns the actual legacy-to-capture conversion.
        retained_capture(store.conn, account, 10 * DAY, ['song|artist'], source='unknown')
        retained_capture(store.conn, account, 20 * DAY, ['song|artist'], source='unknown')
        with store.conn:
            store.conn.executemany('INSERT INTO settings VALUES (?,?)', [
                ('trend_first_play_watermark', '2'), ('trend_first_play_source', 'history')])
        _assert_first(store, 'song|artist', 'Artist', 10)
    reopened = Store(path)
    with closing(reopened.conn):
        reopened.init_schema()
        _assert_first(reopened, 'song|artist', 'Artist', 10)
        assert list(map(tuple, reopened.conn.execute(
            'SELECT precision,timezone,last_observed_at FROM listening_observations ORDER BY id'))) == [
                ('legacy_unknown', None, 10 * DAY), ('legacy_unknown', None, 20 * DAY)]
        assert not reopened.conn.execute('SELECT * FROM play_events').fetchall()
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_alias_disambiguation_rebuilds_renamed_history(store):
    store.accounts.upsert_account("main", "bridge", None, True)
    first = store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    old = store.tracks.identity_key_for_video("a")
    _history(store, old, 10); _history(store, old, 20)
    _assert_first(store, old, "Artist", 10)
    store.tracks.upsert_track("b", "Song", "Artist", None, 200)
    renamed = store.tracks.catalog_subject(first)["identity_key"]
    assert renamed != old
    assert store.settings.get_setting("trend_first_play_watermark") is None
    _assert_first(store, renamed, "Artist", 10)
    assert old not in store.trends.first_play_map("track")
    assert store.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 2


def test_verified_reassociation_preserves_earliest_history(store):
    store.accounts.upsert_account("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist A", None, 200)
    target = store.tracks.upsert_track("b", "Correct Song", "Artist B", None, 200)
    old, key = store.tracks.identity_key_for_video("a"), store.tracks.identity_key_for_video("b")
    _history(store, old, 10); _history(store, key, 20)
    store.trends.refresh_first_play()
    store.tracks.associate_youtube_item("a", store.tracks.catalog_subject(target)["recording_id"],
        source="verified", evidence={"review": "same recording"}, now=100)
    _assert_first(store, key, "Artist B", 10)
    assert old not in store.trends.first_play_map("track")
    assert store.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 2


def test_interruption_rolls_back_index_and_checkpoint(store, monkeypatch):
    store.accounts.upsert_account("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 20); _assert_first(store, key, "Artist", 20)
    _history(store, key, 10)
    # Current ingestion invalidates the published index before a refresh starts.
    assert store.trends.first_play_map("track") == {}
    watermark = store.settings.get_setting("trend_first_play_watermark")
    before = list(map(tuple, store.conn.execute("SELECT * FROM trend_first_play")))
    original = store.trends.repository._replace_first_play
    def interrupted():
        original()
        raise RuntimeError("interrupted after index writes")
    monkeypatch.setattr(store.trends.repository, "_replace_first_play", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        store.trends.refresh_first_play()
    assert list(map(tuple, store.conn.execute("SELECT * FROM trend_first_play"))) == before
    assert store.settings.get_setting("trend_first_play_watermark") == watermark
    monkeypatch.setattr(store.trends.repository, "_replace_first_play", original)
    _assert_first(store, key, "Artist", 10)


def test_exact_ledger_preserves_earlier_coarse_evidence(store):
    store.accounts.upsert_account("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 10); _assert_first(store, key, "Artist", 10)
    store.history.record_play_event(1, key, "a", 20 * DAY)
    _assert_first(store, key, "Artist", 10)
