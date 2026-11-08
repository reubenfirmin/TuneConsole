"""Playback staging preserves attribution without creating catalog ownership."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.store_fixture_support import numbered_store, numbered_store as installed
from yt_playlist.core import album_playback_context, entity_names, playback_bindings, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import playback_context as preparation
from yt_playlist.core.store import Store


JOBS = (preparation.BINDINGS_JOB, preparation.PENDING_JOB)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Playback preparation does not construct an HTTP application."""


def finish(job, conn, size=16):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Playback context fixture did not finish')


def head(conn, source=preparation.BINDINGS_KEY):
    owner, _, current = preparation.tables(source)
    return conn.execute(f'SELECT g.* FROM {current} h JOIN {owner} g USING(source,generation)').fetchone()


def seed(store):
    first = store.accounts.upsert_account('First', 'first.json', None, True)
    second = store.accounts.upsert_account('Second', 'second.json', None, False)
    for account in (first, second):
        store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 1)
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    binding = {'identity_id': first, 'playlist': 'LM', 'video': 'video',
               'opportunity_id': 'original-menu', 'option_id': 7, 'at': 10}
    bindings = [binding, {**binding, 'opportunity_id': 'later-menu', 'option_id': None, 'at': 11},
                {**binding, 'identity_id': 999, 'playlist': 'unseen-playlist', 'video': 'unadmitted', 'at': 12}]
    pending = {'opportunity_id': 'album-choice', 'action_id': 'original-action', 'browse': 'release',
               'playlist': 'LM', 'identity_id': first, 'at': 13, 'videos': ['video', 'video', 'unadmitted']}
    documents = {source: json.dumps(value, ensure_ascii=False, indent=2)
                 for source, value in zip((preparation.BINDINGS_KEY, preparation.PENDING_KEY), (bindings, pending))}
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', documents.items())
    return documents


@pytest.mark.parametrize('numbered_store', [79, 93], indirect=True)
def test_exact_sources_repeated_bindings_queue_and_unknown_owners_are_retained(numbered_store):
    store, _ = numbered_store
    conn = store.conn
    originals = seed(store)
    counts = {table: conn.execute(f'SELECT count(*) FROM main.{table}').fetchone()[0]
              for table in ('identities', 'playlist_scopes', 'catalog_entries', 'album_provider_ids')}
    for job in JOBS:
        job.install(conn)
        assert job.step(conn, batch_size=1).processed == 1
        finish(job, conn)
    for source, original in originals.items():
        assert preparation.read_original(conn, head(conn, source)) == original
        assert store.settings.get_setting(source) == original
    assert list(map(tuple, conn.execute(f'SELECT position,account_id,unresolved_account_id,playlist_scope_id,'
        f'unresolved_scope_reason,video_id,unresolved_video_reason,opportunity_id,option_id FROM {preparation.BINDINGS} ORDER BY position'))) == [
            (0, 1, None, '@account:1:LM', None, 'video', None, 'original-menu', '7'),
            (1, 1, None, '@account:1:LM', None, 'video', None, 'later-menu', 'None'),
            (2, None, 999, None, 'missing_scope', 'unadmitted', 'unadmitted_video', 'original-menu', '7')]
    assert [row[0] for row in conn.execute(f'SELECT video_id FROM {preparation.ITEMS} ORDER BY position')] == ['video', 'video', 'unadmitted']
    assert tuple(conn.execute(f'SELECT account_id,account_kind,playlist_scope_id,source_browse_id,album_id IS NOT NULL,'
        f'opportunity_id,action_id,has_videos FROM {preparation.INTENTS}').fetchone()) == (
            1, 'recorded', '@account:1:LM', 'release', 1, 'album-choice', 'original-action', 1)
    assert not playback_bindings.available(conn)
    assert album_playback_context.load(conn)['videos'] == ['video', 'video', 'unadmitted']
    assert counts == {table: conn.execute(f'SELECT count(*) FROM main.{table}').fetchone()[0] for table in counts}
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert files.collect_orphans(conn) == 0
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_separate_frozen_upgrade_agrees_on_last_binding_and_pending_queue(installed, tmp_path):
    store, _ = installed
    original = seed(store)
    bindings = json.loads(original[preparation.BINDINGS_KEY])[:2]
    store.settings.set_setting(preparation.BINDINGS_KEY, json.dumps(bindings))
    for job in JOBS:
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'playback-upgrade.db')
    try:
        upgraded.conn.execute('PRAGMA synchronous=NORMAL')
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        native = upgraded.conn.execute('SELECT account_id,playlist_scope_id,video_id,opportunity_id,option_id,bound_at '
                                      'FROM listening_playback_bindings').fetchone()
        staged = store.conn.execute(f'SELECT account_id,playlist_scope_id,video_id,opportunity_id,option_id,bound_at '
                                    f'FROM {preparation.BINDINGS} ORDER BY position DESC LIMIT 1').fetchone()
        assert tuple(native) == tuple(staged)
        assert album_playback_context.load(upgraded.conn) == album_playback_context.load(store.conn)
        assert [row[0] for row in upgraded.conn.execute('SELECT video_id FROM album_playback_intent_items ORDER BY position')] == [
            row[0] for row in store.conn.execute(f'SELECT video_id FROM {preparation.ITEMS} ORDER BY position')]
        assert store.conn.execute("SELECT 1 FROM catalog_entries WHERE video_id='unadmitted'").fetchone() is None
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [94, 102, 107], indirect=True)
def test_installed_typed_playback_readers_use_their_physical_scope_columns(installed):
    store, _ = installed
    conn = store.conn
    account = store.accounts.upsert_account('First', 'first.json', None, True)
    other = store.accounts.upsert_account('Second', 'second.json', None, False)
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    # A typed reader must work on an ordinary historical connection as well
    # as on an admitted serving connection with optional TEMP projections.
    entity_names.clear_predecessor_reads(conn)
    value = {'opportunity_id': 'menu', 'browse': 'release', 'playlist': 'LM',
             'account_id': account, 'at': 12, 'videos': ['video', 'video']}
    with conn:
        album_playback_context.put(conn, value)
        playback_bindings.put(conn, account, 'LM', 'video', 'menu', 7, 12, 100)
    assert album_playback_context.load(conn) == value
    assert playback_bindings.find(conn, account, 'LM', 'video', 13, 100) == {
        'account_id': account, 'playlist': 'LM', 'video': 'video', 'opportunity_id': 'menu', 'option_id': '7', 'at': 12}
    assert playback_bindings.find(conn, other, 'LM', 'video', 13, 100) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('state', ['missing-account', 'null-account', 'empty-videos', 'no-videos', 'cleared'])
def test_pending_absence_and_explicit_empty_values_do_not_invent_ownership(installed, state):
    store, _ = installed
    original = seed(store)
    value = json.loads(original[preparation.PENDING_KEY])
    if state == 'cleared':
        value = None
    elif state == 'missing-account':
        del value['identity_id']
    elif state == 'null-account':
        value['identity_id'] = None
    elif state == 'empty-videos':
        value['videos'] = []
    else:
        del value['videos']
    raw = json.dumps(value)
    store.settings.set_setting(preparation.PENDING_KEY, raw)
    preparation.PENDING_JOB.install(store.conn)
    finish(preparation.PENDING_JOB, store.conn)
    assert preparation.read_original(store.conn, head(store.conn, preparation.PENDING_KEY)) == raw
    row = store.conn.execute(f'SELECT * FROM {preparation.INTENTS}').fetchone()
    if state == 'cleared':
        assert row is None
        assert head(store.conn, preparation.PENDING_KEY)['document_kind'] == 'cleared'
    elif state in ('missing-account', 'null-account'):
        assert row['account_kind'] == ('missing' if state == 'missing-account' else 'null')
        assert row['account_id'] is row['unresolved_account_id'] is row['playlist_scope_id'] is None
        assert row['unresolved_scope_reason'] == 'missing_account'
    else:
        assert row['has_videos'] == int(state == 'empty-videos')
        assert head(store.conn, preparation.PENDING_KEY)['item_count'] == 0


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('job,source,owner', [
    (preparation.BINDINGS_JOB, preparation.BINDINGS_KEY, preparation.BINDINGS),
    (preparation.PENDING_JOB, preparation.PENDING_KEY, preparation.ITEMS),
])
def test_large_contexts_checkpoint_individual_members(installed, job, source, owner):
    store, _ = installed
    original = seed(store)
    value = json.loads(original[source])
    if source == preparation.BINDINGS_KEY:
        value = [{**value[0], 'option_id': position} for position in range(1200)]
    else:
        value['videos'] = ['video'] * 1200
    store.settings.set_setting(source, json.dumps(value))
    # Preparation owns a SQLite connection, independently of Store's serving
    # diagnostics. Keep the same populated WAL file and real bounded commits.
    with closing(sqlite3.connect(store._db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=NORMAL')
        job.install(conn)
        prior = 0
        for _ in range(15):
            progress = job.step(conn, batch_size=128)
            assert 0 <= progress.processed - prior <= 128
            prior = progress.processed
            if progress.status == 'prepared':
                break
        else:
            pytest.fail('Large playback context did not finish')
        assert conn.execute(f'SELECT count(*) FROM {owner}').fetchone()[0] == 1200


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_writer_reference_changes_and_cross_job_renames_reconcile(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    for job in JOBS:
        job.install(conn)
        finish(job, conn)
    previous = head(conn, preparation.PENDING_KEY)
    pending = album_playback_context.load(conn)
    with conn:
        album_playback_context.put(conn, {**pending, 'videos': []})
    finish(preparation.PENDING_JOB, conn)
    assert head(conn, preparation.PENDING_KEY)['item_count'] == 0
    assert preparation.read_original(conn, previous) == original[preparation.PENDING_KEY]
    store.tracks.upsert_track('unadmitted', 'Later', 'Artist', 'Later Album', 10, album_browse_id='later-release')
    assert preparation.BINDINGS_JOB.progress(conn).prepared_revision is None
    finish(preparation.BINDINGS_JOB, conn)
    assert conn.execute(f'SELECT unresolved_video_reason FROM {preparation.BINDINGS} '
        'WHERE generation=? AND video_id=?', (head(conn)['generation'], 'unadmitted')).fetchone()[0] is None
    # Both keys are captured on rename; draining one job must not delete the
    # other job's still-retained head before that job reconciles its own input.
    retained = dict(head(conn, preparation.PENDING_KEY))
    with conn:
        conn.execute('DELETE FROM settings WHERE key=?', (preparation.PENDING_KEY,))
        conn.execute('UPDATE settings SET key=? WHERE key=?', (preparation.PENDING_KEY, preparation.BINDINGS_KEY))
    finish(preparation.BINDINGS_JOB, conn)
    assert head(conn) is None
    assert dict(head(conn, preparation.PENDING_KEY)) == retained
    assert preparation.PENDING_JOB.progress(conn).prepared_revision is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_independent_failure_does_not_block_regular_ddl_or_pending_updates(installed):
    store, _ = installed
    seed(store)
    store.settings.set_setting(preparation.BINDINGS_KEY, '{')
    for job in JOBS:
        job.install(store.conn)
    with pytest.raises(ValueError):
        finish(preparation.BINDINGS_JOB, store.conn)
    assert finish(preparation.PENDING_JOB, store.conn).status == 'prepared'
    regular.migrate(store.conn)
    store.settings.set_setting(preparation.BINDINGS_KEY, '[]')
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.BINDINGS_JOB.step(store.conn)
    preparation.BINDINGS_JOB.step(store.conn, retry=True)
    finish(preparation.BINDINGS_JOB, store.conn)
    assert head(store.conn)['item_count'] == 0


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_work_allows_concurrent_source_updates_and_rejects_stale_plan(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    seed(store)
    job = preparation.PENDING_JOB
    job.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute('UPDATE settings SET value=? WHERE key=?', ('null', preparation.PENDING_KEY))
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert job.step(conn, batch_size=1).input_revision == 1
        assert head(conn, preparation.PENDING_KEY) is None
        finish(job, conn)
        assert head(conn, preparation.PENDING_KEY)['document_kind'] == 'cleared'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_member_head_and_disk_failure_preserve_checkpoints(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    job = preparation.PENDING_JOB
    job.install(conn)
    checkpoint = job.step(conn, batch_size=1)
    def interrupted(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled playback member interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupted).step(conn, batch_size=1)
    assert job.progress(conn) == checkpoint
    assert not conn.execute(f'SELECT 1 FROM {preparation.ITEMS}').fetchone()
    def interrupted_head(*args):
        preparation.complete(*args)
        raise KeyboardInterrupt('controlled playback head interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, complete=interrupted_head).step(conn)
    assert job.progress(conn) == checkpoint
    assert head(conn, preparation.PENDING_KEY) is None
    publish = files.publish
    def failed(*args):
        raise OSError('controlled playback disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled playback disk failure'):
        job.step(conn)
    assert job.progress(conn).status == 'failed'
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
        assert preparation.read_original(reopened.conn, head(reopened.conn, preparation.PENDING_KEY)) == original[preparation.PENDING_KEY]
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('source,raw', [
    (preparation.BINDINGS_KEY, '{}'),
    (preparation.BINDINGS_KEY, '[{"identity_id":1,"account_id":2}]'),
    (preparation.BINDINGS_KEY, '[{"account_id":true,"playlist":"LM","video":"v","opportunity_id":"o","option_id":1,"at":1}]'),
    (preparation.PENDING_KEY, '{"playlist":"LM","browse":"b","opportunity_id":"o","at":NaN}'),
    (preparation.PENDING_KEY, '{"playlist":"LM","browse":"b","opportunity_id":"o","at":1,"videos":[null]}'),
])
def test_invalid_contexts_fail_durably_without_changing_authoritative_source(installed, source, raw):
    store, _ = installed
    store.settings.set_setting(source, raw)
    job = preparation.BINDINGS_JOB if source == preparation.BINDINGS_KEY else preparation.PENDING_JOB
    job.install(store.conn)
    with pytest.raises(ValueError):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(source) == raw
    regular.migrate(store.conn)
