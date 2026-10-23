"""Identity changes cannot strand history behind an already advanced first-play checkpoint."""
import pytest

from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.rec.trend_rollups import _build_first_play_index

DAY = 86400


def _history(store, key, day):
    return store.history.record_history_plays(1, day * DAY, [key], source_timezone="UTC")


def _assert_first(store, key, artist, day):
    _build_first_play_index(store)
    assert store.trends.first_play_map("track")[key] == day
    assert store.trends.first_play_map("artist")[artist] == day
    assert store.settings.get_setting('trend_first_play_watermark') is None
    assert store.settings.get_setting('trend_first_play_source') == 'listening_v33'


@pytest.mark.parametrize("old_version", [7, 15])
def test_upgrade_rebuilds_populated_history_only_checkpoint(tmp_path, old_version):
    path = tmp_path / "history.db"
    store = Store(path)
    migrate(store.conn, registry=MIGRATIONS[:old_version])
    store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
    if old_version < 8:
        store.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key) VALUES ('v','Song','Artist','song|artist')")
    else:
        # Seed version 15 using its own schema. Today's repositories require metadata
        # provenance tables introduced later, so they cannot construct this fixture.
        store.conn.execute("INSERT INTO recordings(id,identity_key,title,artist) VALUES (1,'song|artist','Song','Artist')")
        store.conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist,duration_s,available) VALUES ('v',1,'Song','Artist',200,1)")
        store.conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v')")
        store.conn.execute("INSERT INTO library_memberships(entry_id,origin) VALUES (1,'legacy_library')")
        store.conn.execute("INSERT INTO recording_aliases(alias,recording_id,source) VALUES ('song|artist',1,'legacy_library')")
    store.conn.execute("INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (1,1,?)", (10 * DAY,))
    store.conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'song|artist')")
    store.conn.execute("INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (2,1,?)", (20 * DAY,))
    store.conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (2,'song|artist')")
    store.conn.execute("INSERT INTO trend_first_play VALUES ('track','song|artist',10,?,'history')", (10 * DAY,))
    store.conn.execute("INSERT INTO trend_first_play VALUES ('artist','Artist',10,?,'history')", (10 * DAY,))
    store.conn.execute("INSERT INTO settings VALUES ('trend_first_play_watermark','2')")
    store.conn.commit()
    store.init_schema()
    key = store.tracks.identity_key_for_video("v")
    _assert_first(store, key, "Artist", 10)
    store.conn.close()
    reopened = Store(path); reopened.init_schema()
    _assert_first(reopened, key, "Artist", 10)
    assert reopened.conn.execute("SELECT COUNT(*) FROM listening_observations WHERE precision='legacy_unknown'").fetchone()[0] == 2
    assert reopened.conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 0
    reopened.conn.close()


def test_alias_disambiguation_rebuilds_renamed_history(store):
    store.identities.upsert_identity("main", "bridge", None, True)
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
    store.identities.upsert_identity("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist A", None, 200)
    target = store.tracks.upsert_track("b", "Correct Song", "Artist B", None, 200)
    old, key = store.tracks.identity_key_for_video("a"), store.tracks.identity_key_for_video("b")
    _history(store, old, 10); _history(store, key, 20)
    _build_first_play_index(store)
    store.tracks.associate_youtube_item("a", store.tracks.catalog_subject(target)["recording_id"],
        source="verified", evidence={"review": "same recording"}, now=100)
    _assert_first(store, key, "Artist B", 10)
    assert old not in store.trends.first_play_map("track")
    assert store.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 2


def test_interruption_rolls_back_index_and_checkpoint(store, monkeypatch):
    store.identities.upsert_identity("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 20); _assert_first(store, key, "Artist", 20)
    _history(store, key, 10)
    # Current ingestion invalidates the published index before a refresh starts.
    assert store.trends.first_play_map("track") == {}
    watermark = store.settings.get_setting("trend_first_play_watermark")
    before = list(map(tuple, store.conn.execute("SELECT * FROM trend_first_play")))
    original = store.trends._rebuild_artist_first_play
    def interrupted():
        original()
        raise RuntimeError("interrupted after index writes")
    monkeypatch.setattr(store.trends, "_rebuild_artist_first_play", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        _build_first_play_index(store)
    assert list(map(tuple, store.conn.execute("SELECT * FROM trend_first_play"))) == before
    assert store.settings.get_setting("trend_first_play_watermark") == watermark
    monkeypatch.setattr(store.trends, "_rebuild_artist_first_play", original)
    _assert_first(store, key, "Artist", 10)


def test_exact_ledger_preserves_earlier_coarse_evidence(store):
    store.identities.upsert_identity("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 10); _assert_first(store, key, "Artist", 10)
    store.history.record_play_event(1, key, "a", 20 * DAY)
    _assert_first(store, key, "Artist", 10)
