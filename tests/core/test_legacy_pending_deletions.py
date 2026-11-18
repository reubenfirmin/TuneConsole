"""Real predecessor conversion and controlled interrupted deletion transports."""
import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import (
    install_numbered_input,
    mock_regular_numbered_input,
    native_sql_calls,
    reuse_reporting_reader,
    without_query_timing,
)
from tests.test_undo_recovery import RecoveryClient
from yt_playlist.core import action_work, fao, schema_inspection
from yt_playlist.core.migrations import migrate, registry, regular
from yt_playlist.core.store import Store
from yt_playlist.library import executor, playlist_deletion
from yt_playlist.repos.models import Playlist


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Deletion SQL and provider coordination do not start an HTTP app."""


@pytest.fixture(autouse=True)
def controlled_provider_retry_clock(monkeypatch):
    from yt_playlist.util.retry import with_retry

    # Preserve all actual failed reads and bounded retries. Provider backoff
    # time is below the uncertain-write/readback boundary exercised here.
    monkeypatch.setattr(executor, 'with_retry',
                        lambda fn, **options: with_retry(fn, sleep=lambda delay: None, **options))


@pytest.fixture
def deletion_inputs(monkeypatch, native_sql_calls, without_query_timing):
    """Completed lower SQL; each test owns its actual conversion and reopens."""
    bound_catalog(monkeypatch)
    with ExitStack() as resources:
        templates = {}

        def get(version):
            if version not in templates:
                schema = resources.enter_context(closing(sqlite3.connect(':memory:')))
                install_numbered_input(schema, version)
                assert schema.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
                assert not schema.execute('SELECT 1 FROM main.actions').fetchone()
                schema.execute('PRAGMA query_only=ON')
                templates[version] = schema
            return templates[version]

        yield get


class InterruptedDeletion(RecoveryClient):
    def __init__(self):
        super().__init__({'SOURCE':['a','b'],'KEEP':['a','b']})
        self.attempts = []
        self.reads = []

    def delete_playlist(self, playlistId):
        self.attempts.append(playlistId)
        raise SystemExit('interrupted after transport invocation')

    def get_playlist(self, playlistId, limit=None):
        self.reads.append((playlistId,limit))
        return super().get_playlist(playlistId,limit)


def _step(store, action):
    row, = store.conn.execute('SELECT * FROM action_steps WHERE action_id=?',(action,)).fetchall()
    return row


def _writes(client):
    return (list(client.attempts),list(client.added),list(client.removed),list(client.created))


def _merge(store, account, memberships, client, now):
    executor.apply_result(store,{account:client},memberships,['a','b','c'],'all',now)


def private_store(path):
    store = Store(path)
    # Keep real on-disk reopens and uncertain-write recovery, independently
    # of SQLite's synchronization against a host power failure.
    store.conn.execute('PRAGMA synchronous=OFF')
    return store


def test_current_unsent_intent_survives_repair_and_admits_keeper_write(tmp_path,monkeypatch,
                                                                  deletion_inputs):
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path/'home'))
    path = tmp_path/'unsent.db'
    store = private_store(path)
    try:
        deletion_inputs(107).backup(store.conn)
        store._bind_repositories()
        account = store.accounts.upsert_account('Owner','owner.json',None,True)
        memberships = [store.playlists.upsert_playlist(account,remote,remote,2,'',1)
                       for remote in ('SOURCE','KEEP')]
        client = InterruptedDeletion()
        def before_admission(*args,**kwargs):
            raise SystemExit('before admission')
        with monkeypatch.context() as patch:
            patch.setattr(store.actions,'begin_deletion',before_admission)
            with pytest.raises(SystemExit,match='before admission'):
                executor.delete_playlist(store,memberships[0],client,2)
        action, = store.actions.get_actions()
        step = dict(_step(store,action.id))
        assert (step['phase'],step['outcome'])==('pending','not_sent')
        assert not client.attempts
        store.conn.close()
        store = private_store(path)
        store.init_schema()
        assert dict(_step(store,action.id))==step
        _merge(store,account,memberships,client,3)
        assert not client.attempts
        assert [[row['videoId'] for row in client._tracks[remote]] for remote in ('SOURCE','KEEP')]==[
            ['a','b','c'],['a','b','c']]
    finally:
        store.conn.close()
