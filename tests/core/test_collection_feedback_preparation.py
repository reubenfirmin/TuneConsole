"""Retained feedback and auth maps have independent, resumable preparation."""
from dataclasses import replace
import json

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import account_auth_state, album_feedback_storage, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import collection_feedback as preparation
from yt_playlist.core.store import Store


JOBS = (preparation.FEEDBACK_JOB, preparation.AUTH_JOB)


def finish(job, conn, size=16):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Collection feedback fixture did not finish')


def head(conn, source='album_feedback'):
    owner, _, current = preparation.tables(source)
    return conn.execute(f'SELECT g.* FROM {current} h JOIN {owner} g USING(source,generation)').fetchone()


def seed(store):
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    with store.conn:
        store.conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id,is_primary,equivalence_digest,verified_at) "
            "SELECT 'youtube','alias',album_id,0,?,1 FROM album_provider_ids WHERE provider='youtube' AND provider_id='release'", ('a'*64,))
    feedback = {'release': {'kind': 'more', 'at': 12, 'opportunity_id': 'menu', 'action_id': None},
                'alias': {'kind': 'reject', 'at': 13, 'action_id': 'original-action'},
                'missing-release': {'kind': 'known', 'at': 14, 'opportunity_id': 7, 'action_id': True}}
    auth = {str(account): 'Exact été label', '999': 'Historical account', '998': ''}
    documents = {source: json.dumps(value, ensure_ascii=False, indent=2)
                 for source, value in zip(preparation.KEYS, (feedback, auth))}
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', documents.items())
    return documents


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_independent_jobs_keep_exact_source_judgments_accounts_and_unknowns(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    for job in JOBS:
        job.install(conn)
        assert job.step(conn, batch_size=1).processed == 1
        finish(job, conn)
    for source, raw in original.items():
        assert preparation.read_original(conn, head(conn, source)) == raw
        assert conn.execute('SELECT value FROM settings WHERE key=?', (source,)).fetchone()[0] == raw
    rows = conn.execute(f'SELECT source_browse_id,response,created_at,account_id,legacy_author_unknown '
        f'FROM {preparation.RESPONSES} ORDER BY position').fetchall()
    assert list(map(tuple, rows)) == [('release', 'more', 12, None, 1), ('alias', 'reject', 13, None, 1),
                                    ('missing-release', 'known', 14, None, 1)]
    albums = conn.execute(f'SELECT album_id,unresolved_reason FROM {preparation.RESPONSES} ORDER BY position').fetchall()
    assert albums[0][0] == albums[1][0] is not None
    assert tuple(albums[2]) == (None, 'missing_album')
    assert [tuple(row) for row in conn.execute(f'SELECT raw_account_id,account_id,unresolved_account_id,label '
        f'FROM {preparation.AUTH} ORDER BY position')] == [
            ('1', 1, None, 'Exact été label'), ('999', None, 999, 'Historical account'), ('998', None, 998, '')]
    assert not album_feedback_storage.available(conn)
    assert not account_auth_state.available(conn)
    assert not conn.execute("SELECT 1 FROM album_provider_ids WHERE provider_id='missing-release'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert files.collect_orphans(conn) == 0
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_frozen_upgrade_on_a_separate_copy_retains_the_same_decisions(installed, tmp_path):
    store, _ = installed
    original = seed(store)
    for job in JOBS:
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'collection-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        assert account_auth_state.read(upgraded.conn) == {int(key): value for key, value in json.loads(original['auth_expired_identities']).items()}
        native = [tuple(row) for row in upgraded.conn.execute('SELECT source_browse_id,response,created_at,opportunity_id,action_id,'
            'account_id,legacy_author_unknown FROM album_feedback ORDER BY created_at,source_browse_id')]
        staged = [tuple(row) for row in store.conn.execute(f'SELECT source_browse_id,response,created_at,opportunity_id,action_id,'
            f'account_id,legacy_author_unknown FROM {preparation.RESPONSES} ORDER BY position')]
        assert native == staged
        assert native[-1][3:5] == ('7', '1')
        assert upgraded.conn.execute("SELECT withdrawn_at FROM album_feedback WHERE source_browse_id='release'").fetchone()[0] == 13
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('source,job,owner', [
    ('album_feedback', preparation.FEEDBACK_JOB, preparation.RESPONSES),
    ('auth_expired_identities', preparation.AUTH_JOB, preparation.AUTH),
])
def test_long_maps_commit_at_most_the_requested_number_of_members(installed, source, job, owner):
    store, _ = installed
    value = ({'release-' + str(number): {'kind': 'more', 'at': number} for number in range(600)}
             if source == 'album_feedback' else {str(number + 1): 'Account' for number in range(600)})
    store.settings.set_setting(source, json.dumps(value))
    job.install(store.conn)
    prior = 0
    for _ in range(10):
        progress = job.step(store.conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long map did not finish')
    assert store.conn.execute(f'SELECT count(*) FROM {owner}').fetchone()[0] == 600
    assert json.loads(preparation.read_original(store.conn, head(store.conn, source))) == value


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_of_one_job_does_not_block_the_other_or_regular_schema_changes(installed):
    store, _ = installed
    seed(store)
    store.settings.set_setting('album_feedback', '{')
    preparation.FEEDBACK_JOB.install(store.conn)
    preparation.AUTH_JOB.install(store.conn)
    with pytest.raises(ValueError):
        finish(preparation.FEEDBACK_JOB, store.conn)
    regular.migrate(store.conn)
    assert finish(preparation.AUTH_JOB, store.conn).status == 'prepared'
    assert preparation.FEEDBACK_JOB.progress(store.conn).status == 'failed'
    store.settings.set_setting('album_feedback', '{}')
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.FEEDBACK_JOB.step(store.conn)
    preparation.FEEDBACK_JOB.step(store.conn, retry=True)
    finish(preparation.FEEDBACK_JOB, store.conn)
    assert preparation.read_original(store.conn, head(store.conn)) == '{}'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_partial_source_replacement_rename_and_reference_edits_reconcile(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    job = preparation.FEEDBACK_JOB
    job.install(conn)
    job.step(conn, batch_size=1)
    store.settings.set_setting('album_feedback', '{}')
    finish(job, conn)
    assert head(conn)['item_count'] == 0
    store.settings.set_setting('album_feedback', original['album_feedback'])
    finish(job, conn)
    previous = head(conn)
    with conn:
        conn.execute("DELETE FROM album_provider_ids WHERE provider='youtube' AND provider_id='alias'")
    assert job.progress(conn).prepared_revision is None
    finish(job, conn)
    assert conn.execute(f'SELECT unresolved_reason FROM {preparation.RESPONSES} '
        'WHERE generation=? AND source_browse_id=?', (head(conn)['generation'], 'alias')).fetchone()[0] == 'missing_album'
    assert preparation.read_original(conn, previous) == original['album_feedback']
    with conn:
        conn.execute("UPDATE settings SET key='unrelated',value='opaque' WHERE key='album_feedback'")
    finish(job, conn)
    assert head(conn) is None
    assert preparation.read_original(conn, previous) == original['album_feedback']


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_io_does_not_reserve_a_writer_and_stale_plans_are_rejected(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    seed(store)
    job = preparation.FEEDBACK_JOB
    job.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE settings SET value='{}' WHERE key='album_feedback'")
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert job.step(conn, batch_size=1).input_revision == 1
        assert head(conn) is None
        finish(job, conn)
        assert preparation.read_original(conn, head(conn)) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_rename_to_another_jobs_key_does_not_withdraw_its_prior_generation(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    for job in JOBS:
        job.install(conn)
        finish(job, conn)
    previous = head(conn, 'auth_expired_identities')
    with conn:
        conn.execute("DELETE FROM settings WHERE key='auth_expired_identities'")
        conn.execute("UPDATE settings SET key='auth_expired_identities' WHERE key='album_feedback'")
    finish(preparation.FEEDBACK_JOB, conn)
    assert head(conn) is None
    assert dict(head(conn, 'auth_expired_identities')) == dict(previous)
    assert preparation.read_original(conn, previous) == original['auth_expired_identities']
    assert preparation.AUTH_JOB.progress(conn).prepared_revision is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_member_and_failed_disk_keep_commits_for_explicit_reopened_retry(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    job = preparation.FEEDBACK_JOB
    job.install(conn)
    checkpoint = job.step(conn, batch_size=1)
    copy = preparation.copy
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled collection member interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupted).step(conn, batch_size=1)
    assert job.progress(conn) == checkpoint
    assert not conn.execute(f'SELECT 1 FROM {preparation.RESPONSES}').fetchone()
    publish = files.publish
    def failed(*args):
        raise OSError('controlled collection disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled collection disk failure'):
        job.step(conn)
    assert job.progress(conn).status == 'failed'
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
        assert preparation.read_original(reopened.conn, head(reopened.conn)) == original['album_feedback']
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('source,raw', [
    ('album_feedback', '{"release":{"kind":"more","at":NaN}}'),
    ('album_feedback', '{"release":{"kind":"more","at":1,"extra":2}}'),
    ('auth_expired_identities', '{"01":"Ambiguous"}'),
    ('auth_expired_identities', '{"9223372036854775808":"Overflow"}'),
    ('auth_expired_identities', '{"1":null}'),
])
def test_invalid_maps_fail_durably_and_remain_authoritative(installed, source, raw):
    store, _ = installed
    store.settings.set_setting(source, raw)
    job = preparation.FEEDBACK_JOB if source == 'album_feedback' else preparation.AUTH_JOB
    job.install(store.conn)
    with pytest.raises(ValueError):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(source) == raw
    regular.migrate(store.conn)
