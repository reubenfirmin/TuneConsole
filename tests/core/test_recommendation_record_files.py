"""Deliberate SQL/FAO interruption and restart checks for display publications."""
from contextlib import closing
import sqlite3

import pytest

from tests.store_fixture_support import file_consumer_store, native_sql_calls, without_query_timing
from yt_playlist.core import fao, recommendation_content
from yt_playlist.core.store import Store

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
RECEIPTS = ('SELECT json_extract(owner_id,\'$[0]\') id,file_status,content_digest '
            "FROM collection_file_writes WHERE kind='recommendation-output'")


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These persistence checks do not construct the HTTP application."""


def save(store, workflow, marker, now):
    surface = 'home_prepared' if workflow == 'home' else 'listening_edition'
    store.rec_proposals.put_proposals(surface, {'title': marker}, now)


def read(store, workflow):
    surface = 'home_prepared' if workflow == 'home' else 'listening_edition'
    return store.rec_proposals.get_proposals(surface)


@pytest.mark.parametrize('workflow', ['home', 'listening'])
@pytest.mark.parametrize('failure', ['file', 'status'])
def test_interrupted_display_retains_the_prior_head_and_pending_identity_across_restart(
        file_consumer_store, monkeypatch, workflow, failure):
    store = file_consumer_store
    save(store, workflow, 'first', 10)
    owner = store.home_cache if workflow == 'home' else store.listening_analytics
    operation = 'publish' if workflow == 'home' else 'publish_files'
    target, method = (owner.files, 'publish_reserved') if failure == 'file' else (owner.repository, operation)

    def interrupted(*args, **kwargs):
        raise OSError('display publication interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(target, method, interrupted)
        with pytest.raises(OSError, match='display publication interrupted'):
            save(store, workflow, 'second', 20)
    assert read(store, workflow) == {'title': 'first'}
    assert not store.conn.in_transaction
    with closing(sqlite3.connect(store._db_path)) as observer:
        observer.row_factory = sqlite3.Row
        rows = [dict(row) for row in observer.execute(RECEIPTS + ' ORDER BY id')]
    assert [row['file_status'] for row in rows] == ['ready', 'pending']
    pending = recommendation_content.record_path(owner.files.root, rows[-1]['id'])
    assert pending.exists() is (failure == 'status')
    if pending.exists():
        assert fao.read(pending).bytes

    restarted = Store(store._db_path)
    try:
        restarted.init_schema()
        assert read(restarted, workflow) == {'title': 'first'}
        save(restarted, workflow, 'second', 30)
        assert read(restarted, workflow) == {'title': 'second'}
        row = restarted.conn.execute(RECEIPTS + ' ORDER BY id DESC LIMIT 1').fetchone()
        assert row['file_status'] == 'ready'
        path = recommendation_content.record_path(owner.files.root, row['id'])
        assert path.stem == str(row['id']) and path.stem != row['content_digest']
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restarted.conn.close()


def test_reused_home_preparation_cannot_replace_a_later_head_with_an_empty_publication(file_consumer_store):
    store = file_consumer_store
    raw = '{"title":"first"}'
    store.rec_proposals.put_encoded('home_prepared', raw, 10)
    with store.rec_proposals.prepare_home(raw, 11) as prepared:
        assert not prepared.file_ids
        save(store, 'home', 'winner', 12)
        with pytest.raises(ValueError, match='pending SQL identities'):
            store.rec_proposals.publish_home(prepared)
    assert read(store, 'home') == {'title': 'winner'}
    assert store.conn.execute("SELECT count(*) FROM collection_file_writes "
                              "WHERE display_key GLOB 'prepared_home:*' AND kind='recommendation-output'").fetchone()[0] == 1
    assert not store.conn.in_transaction
