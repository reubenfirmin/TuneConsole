"""Provider revisions survive bounded publication, restart and newer attempts."""
from contextlib import contextmanager
import json

import pytest

from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.repos import enrichment_state

ORIGINAL = {'deezer': {'fingerprint': 'original', 'status': 'available'}}
CHANGED = {'deezer': {'fingerprint': 'changed', 'status': 'available'}}


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'providers.db')
    store.init_schema()
    store.enrichment_state.reconcile_provider_configuration(ORIGINAL)
    yield store
    store.conn.close()


def completed(store, count=2, provider='musicbrainz'):
    ids = [store.tracks.upsert_track(f'song{i}', f'Song {i}', 'Artist', None, 200)
           for i in range(count)]
    store.enrichment_queue.mark_enriched(ids, 10)
    rows = [dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone())
            for entry in ids]
    for row in rows:
        store.enrichment_state.record_enrichment_attempt(row['identity_key'], provider, {}, now=10)
    return rows


def jobs(store):
    return {row[0] for row in store.conn.execute(
        "SELECT recording_id FROM enrichment_provider_jobs WHERE provider='deezer'")}


def test_unchanged_configuration_catches_up_later_recordings_without_rewriting_configuration(database):
    rows = completed(database)
    database.enrichment_state.reconcile_provider_configuration(ORIGINAL)
    assert jobs(database) == {row['recording_id'] for row in rows}
    for row in rows:
        database.enrichment_state.record_enrichment_attempt(row['identity_key'], 'deezer', {}, now=20)
    before = database.conn.total_changes
    database.enrichment_state.reconcile_provider_configuration(ORIGINAL)
    assert database.conn.total_changes == before
    # Catch-up is independent of a configuration change, including after a restart.
    new = database.tracks.upsert_track('later', 'Later', 'Artist', None, 200)
    database.enrichment_queue.mark_enriched([new], 30)
    key = database.conn.execute('SELECT identity_key FROM catalog_tracks WHERE id=?', (new,)).fetchone()[0]
    database.enrichment_state.record_enrichment_attempt(key, 'musicbrainz', {}, now=30)
    database.enrichment_state.reconcile_provider_configuration(ORIGINAL)
    rid = database.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (new,)).fetchone()[0]
    assert jobs(database) == {rid}


@pytest.mark.parametrize('change', ['removed', 'requeued', 'answered', 'retry', 'disabled', 'reconfigured'])
def test_catchup_revalidates_membership_completion_attempts_and_configuration(database, monkeypatch, change):
    row = completed(database, count=1)[0]
    original = database.read_view
    mutations = []

    @contextmanager
    def changed_reader():
        with original() as reader:
            assert reader.conn is not database.conn
            yield reader
        if mutations:
            return
        mutations.append(change)
        if change in ('answered', 'retry'):
            database.enrichment_state.record_enrichment_attempt(row['identity_key'], 'deezer', {},
                                                                retry=change == 'retry', now=20)
        elif change == 'requeued':
            database.enrichment_queue.requeue_enrichment([row['id']])
        else:
            with database.conn:
                if change == 'removed':
                    database.conn.execute('DELETE FROM library_memberships WHERE entry_id=?', (row['id'],))
                elif change == 'disabled':
                    database.conn.execute("UPDATE enrichment_provider_config SET status='disabled'")
                else:
                    database.conn.execute("UPDATE enrichment_provider_config SET fingerprint='another'")

    monkeypatch.setattr(database, 'read_view', changed_reader)
    database.enrichment_state.reconcile_provider_configuration(ORIGINAL)
    assert mutations == [change]
    assert jobs(database) == ({row['recording_id']} if change == 'retry' else set())
    if change == 'retry':
        assert database.enrichment_state.enrichment_provider_state(row['recording_id'])['deezer']['status'] == 'retry'
    assert not database.conn.in_transaction


def test_partial_configuration_refresh_resumes_after_restart_without_requeueing_finished_jobs(database, monkeypatch):
    rows = completed(database, provider='deezer')
    monkeypatch.setattr(enrichment_state, 'RECONCILE_BATCH', 1)
    original = database.enrichment_state._advance_provider_refresh

    def interrupted(provider, current):
        assert original(provider, current)
        raise WorkClosing('injected shutdown between refresh batches')

    monkeypatch.setattr(database.enrichment_state, '_advance_provider_refresh', interrupted)
    with pytest.raises(WorkClosing):
        database.enrichment_state.reconcile_provider_configuration(CHANGED)
    assert jobs(database) == {rows[0]['recording_id']}
    refresh = json.loads(database.settings.get_setting(enrichment_state.REFRESH_KEY + 'deezer'))
    assert refresh['cursor'] == rows[0]['recording_id']
    database.enrichment_state.record_enrichment_attempt(rows[0]['identity_key'], 'deezer', {}, now=20)
    restarted = Store(database._db_path)
    restarted.init_schema()
    try:
        restarted.enrichment_state.reconcile_provider_configuration(CHANGED)
        assert jobs(restarted) == {rows[1]['recording_id']}
        assert restarted.settings.get_setting(enrichment_state.REFRESH_KEY + 'deezer') is None
        assert restarted.enrichment_state.enrichment_provider_state(rows[0]['recording_id'])['deezer']['at'] == 20
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        restarted.conn.close()


@pytest.mark.parametrize('retry', [False, True])
def test_refresh_preserves_attempt_and_cooldown_committed_after_configuration_change(database, monkeypatch, retry):
    row = completed(database, count=1, provider='deezer')[0]
    original = database.enrichment_state._advance_provider_refresh
    expected = None

    def newer_attempt(provider, current):
        nonlocal expected
        if expected is None:
            database.enrichment_state.record_enrichment_attempt(row['identity_key'], 'deezer', {'genre': 'House'},
                retry=retry, cooldown=retry, now=20)
            expected = database.enrichment_state.enrichment_provider_state(row['recording_id'])['deezer']
        return original(provider, current)

    monkeypatch.setattr(database.enrichment_state, '_advance_provider_refresh', newer_attempt)
    database.enrichment_state.reconcile_provider_configuration(CHANGED)
    assert database.enrichment_state.enrichment_provider_state(row['recording_id'])['deezer'] == expected
    assert jobs(database) == ({row['recording_id']} if retry else set())
    assert bool(database.enrichment_state.enrichment_cooldowns()) == retry


def test_refresh_intent_survives_temporarily_missing_membership(database):
    row = completed(database, count=1, provider='deezer')[0]
    with database.conn:
        database.conn.execute('DELETE FROM library_memberships WHERE entry_id=?', (row['id'],))
    database.enrichment_state.reconcile_provider_configuration(CHANGED)
    assert jobs(database) == {row['recording_id']}
    assert database.enrichment_reports.queue_remaining() == 0
    database.tracks.upsert_track('song0', 'Song 0', 'Artist', None, 200)
    assert database.enrichment_reports.queue_remaining() == 1
