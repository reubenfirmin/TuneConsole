"""Listening capture remains usable before the subject/name conversions."""
import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import entity_names


def test_sync_disambiguation_keeps_captured_evidence_on_its_recording(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    entry = store.tracks.upsert_track('first-upload', 'Song', 'Artist', None, 123)
    original_key = store.tracks.identity_key_for_video('first-upload')
    stamp = 1704844800
    store.history.record_play_event(account, original_key, 'first-upload', stamp,
                                    source_timezone='UTC')
    store.history.record_history_plays(account, stamp - 86400, [(original_key, 'today')],
                                       source_timezone='UTC')
    subject_columns = {'recording_id', 'identity_key', 'unresolved_subject_key'}

    def evidence():
        return {table: [{key: value for key, value in dict(row).items() if key not in subject_columns}
                        for row in store.conn.execute('SELECT * FROM main.' + table + ' ORDER BY id')]
                for table in ('play_events', 'listening_observations')}

    retained = evidence()
    # A second upload with the same title disambiguates the former text alias.
    # This is the catalog upsert used by normal playlist sync, before cutover.
    store.tracks.upsert_track('second-upload', 'Song', 'Artist', None, 125)
    first_recording = store.tracks.catalog_subject(entry)['recording_id']
    first_key = store.tracks.identity_key_for_video('first-upload')
    assert first_key != original_key
    assert first_key != store.tracks.identity_key_for_video('second-upload')
    assert evidence() == retained
    for table in ('play_event_subjects', 'listening_observation_subjects'):
        row, = store.conn.execute('SELECT * FROM ' + table).fetchall()
        assert row['recording_id'] == first_recording
        assert row['identity_key'] == first_key


def test_live_coarse_and_late_upload_resolution_preserve_original_evidence(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 1)
    store.tracks.upsert_track('known', 'Song', 'Artist', None, 123)
    key = store.tracks.identity_key_for_video('known')
    stamp = 1704844800  # 2024-01-10 UTC
    assert store.history.record_play_event(account, key, 'known', stamp, 'LM', source_timezone='UTC')
    assert not store.history.record_play_event(account, key, 'known', stamp + 10, 'LM', 'LIKE')
    assert store.history.record_history_plays(account, stamp + 20, [(key, 'today')], source_timezone='UTC') == 0
    assert store.history.record_history_plays(account, stamp + 30, [(key, 'today')], source_timezone='UTC') == 0
    row, = store.conn.execute('SELECT first_observed_at,last_observed_at FROM listening_observations').fetchall()
    assert tuple(row) == (stamp + 20, stamp + 30)
    # An unrecognized exact upload must not be attributed to a familiar title.
    assert store.history.record_play_event(account, key, 'unrecognized', stamp + 4000, source_timezone='UTC')
    rows = store.conn.execute('SELECT * FROM play_event_subjects ORDER BY id').fetchall()
    assert rows[0]['raw_identity_key'] == key and rows[0]['like_status'] == 'LIKE'
    assert rows[1]['recording_id'] is None and rows[1]['raw_identity_key'] == key
    store.tracks.upsert_track('unrecognized', 'Different song', 'Different artist', None, 234)
    changed = store.conn.execute('SELECT * FROM play_event_subjects WHERE id=?', (rows[1]['id'],)).fetchone()
    assert changed['recording_id'] is not None and changed['recording_id'] != rows[0]['recording_id']
    assert changed['raw_identity_key'] == key
    assert changed['identity_key'] == store.tracks.identity_key_for_video('unrecognized')
    store.history.remove_listening_source('ytm_history', account)
    assert not store.conn.execute('SELECT 1 FROM listening_observations').fetchone()
    assert store.conn.execute('SELECT count(*) FROM main.play_events').fetchone()[0] == 2
    with pytest.raises(ValueError, match='Unknown legacy provenance'):
        store.history.remove_listening_source('unknown', account)


def test_imports_keep_receipts_deduplicate_and_scope_resets_to_one_account(installed):
    store, _ = installed
    first = store.accounts.upsert_account('Main', 'main.json', None, True)
    second = store.accounts.upsert_account('Other', 'other.json', None, False)
    store.tracks.upsert_track('known', 'Song', 'Artist', None, 123)
    key = store.tracks.identity_key_for_video('known')
    stamp = 1704844800
    for account in (first, second):
        store.history.record_history_plays(account, stamp, [key], source_timezone='UTC')
    imported = [('spotify:track:known', stamp, 'Artist', key),
                ('spotify:track:unknown', stamp, 'Unresolved artist', None)]
    assert store.history.import_spotify_listens(first, imported)[1:] == (1, 2)
    assert store.history.import_spotify_listens(first, imported)[1:] == (0, 0)
    assert store.history.import_listening_events(first, [(key, 'known', stamp)], source='takeout')[1] == 0
    receipt = store.history.record_takeout_import('retained-fingerprint', first, stamp + 1,
                                                [{'ts': stamp}], 2)
    assert store.conn.execute('SELECT id FROM main.takeout_imports').fetchone()[0] == receipt
    store.history.reset_play_history(first)
    observations = entity_names.owner(store.conn, 'listening_observations')
    assert [row[0] for row in store.conn.execute(
        f'SELECT {observations.column("account_id")} FROM {observations.table}')] == [second]
    assert store.history.event_play_counts(first) == {key: 1}
    assert store.history.event_play_counts(second) == {}
    assert store.conn.execute('SELECT count(*) FROM main.spotify_history_listens').fetchone()[0] == 2
