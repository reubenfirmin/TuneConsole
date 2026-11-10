"""Real large receipt preparation and continuing writes have distinct owners."""
import json
from types import SimpleNamespace

import pytest

from tests.core.test_workflow_publication import prepare, stage
from tests.core.test_workflow_receipt_preparation import seed
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import schema_inspection
from yt_playlist.core.preparation import workflow_publication as publication, workflow_receipts as preparation
from yt_playlist.library import playlist_gc_retry
from yt_playlist.library.spotify_library import _playlist_transfers


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Receipt SQL and file publication do not start the HTTP lifecycle."""


def test_large_workflow_schema_input_keeps_real_history_and_snapshot_guards(_bounded_installed_schema):
    source = _bounded_installed_schema(93).source
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def workflow(_bounded_installed_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('large-workflow')/'source.db'
    with _bounded_installed_schema(93).open(path) as store, pytest.MonkeyPatch.context() as patch:
        with reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
            values, originals, account, source = seed(store, count=8192)
            yield SimpleNamespace(store=store, values=values, originals=originals, account=account, source=source)


def test_large_workflow_source_keeps_all_ordered_positions_and_opaque_paid_evidence(workflow):
    receipt = workflow.values[workflow.source]['fingerprint:東京']
    assert len(receipt['source_video_ids']) == 8192
    assert receipt['source_video_ids'][:3] == [None, 'repeated-video', 'repeated-video']
    assert receipt['opaque'] == {'paid_result': [1, '東京', None]}
    assert workflow.store.settings.get_setting(workflow.source) == workflow.originals[workflow.source]


@pytest.fixture(scope='module')
def prepared_workflow(workflow):
    for owner in publication.OWNERS.values():
        prepare(owner, workflow.store.conn, size=512)
    return workflow


def test_large_workflow_preparation_keeps_the_full_receipt_without_selecting_it(prepared_workflow):
    state = prepared_workflow
    head = preparation.head(state.store.conn, 'playlists', state.source)
    assert state.store.conn.execute('SELECT count(*) FROM '+preparation.table('playlists', 'items')+
        ' WHERE source=? AND preparation_revision=?', (state.source, head['preparation_revision'])).fetchone()[0] == 8192
    assert all(owner.selected(state.store.conn) is None for owner in publication.OWNERS.values())


def publish_prepared(owner, conn):
    for _ in range(100):
        result = owner.step(conn, batch_size=128)
        if result.complete:
            owner.publish(conn, stage=result.id)
            return result.id
    pytest.fail('Large workflow map did not finish bounded fixture')


@pytest.fixture(scope='module')
def published_workflow(prepared_workflow):
    for owner in publication.OWNERS.values():
        publish_prepared(owner, prepared_workflow.store.conn)
    return prepared_workflow


def test_large_workflow_publication_selects_all_three_independent_receipts(published_workflow):
    state = published_workflow
    for owner in publication.OWNERS.values():
        assert owner.selected(state.store.conn) is not None
    for key, raw in state.originals.items():
        assert state.store.settings.get_setting(key) == raw


@pytest.fixture(scope='module')
def updated_gc(published_workflow):
    state = published_workflow
    store = state.store
    gc, playlists, albums = (publication.OWNERS[name] for name in ('gc', 'playlists', 'albums'))
    state.old_gc = gc.selected(store.conn)
    state.gc_key = playlist_gc_retry.key(state.account, 'PLAYLIST')
    state.playlist = SimpleNamespace(account_id=state.account, provider_playlist_id='PLAYLIST')
    with store.read_snapshot() as snapshot:
        assert playlist_gc_retry.states(snapshot.store) == {state.gc_key: state.values[state.gc_key]}
        updated = playlist_gc_retry.begin(store, state.playlist, 100, state.values[state.gc_key])
        assert not snapshot.publish(lambda: None)
    assert gc.selected(store.conn) is None
    assert playlists.selected(store.conn) is not None and albums.selected(store.conn) is not None
    assert json.loads(store.settings.get_setting(state.gc_key)) == updated
    gc.publish(store.conn, stage=stage(gc, store.conn))
    assert playlist_gc_retry.read(store, state.playlist) == updated
    assert gc.value(store.conn, state.old_gc, state.gc_key) == state.originals[state.gc_key]
    return state


def test_real_gc_write_fences_snapshot_without_withdrawing_other_receipts(updated_gc):
    assert publication.OWNERS['gc'].selected(updated_gc.store.conn) is not None


@pytest.fixture(scope='module')
def updated_transfer(updated_gc):
    state = updated_gc
    store = state.store
    with store.read_snapshot() as snapshot:
        _, state.transfer = _playlist_transfers(snapshot.store, state.account)
        state.transfer['fingerprint:東京']['source_video_ids'][0] = 'newly-confirmed'
        store.settings.set_setting(state.source, json.dumps(state.transfer))
        assert not snapshot.publish(lambda: None)
    assert publication.OWNERS['playlists'].selected(store.conn) is None
    assert publication.OWNERS['gc'].selected(store.conn) is not None
    assert publication.OWNERS['albums'].selected(store.conn) is not None
    return state


def test_real_transfer_write_fences_snapshot_without_withdrawing_gc_or_album_receipts(updated_transfer):
    assert _playlist_transfers(updated_transfer.store, updated_transfer.account)[1] == updated_transfer.transfer


@pytest.fixture(scope='module')
def prepared_transfer(updated_transfer):
    prepare(publication.OWNERS['playlists'], updated_transfer.store.conn, size=512)
    return updated_transfer


def test_continuing_transfer_preparation_keeps_all_eight_thousand_positions(prepared_transfer):
    state = prepared_transfer
    head = preparation.head(state.store.conn, 'playlists', state.source)
    assert preparation.read_original(state.store.conn, 'playlists', head) == json.dumps(state.transfer)
    assert publication.OWNERS['playlists'].selected(state.store.conn) is None


def test_reconciled_transfer_and_cleared_gc_keep_exact_original_archives(prepared_transfer):
    state = prepared_transfer
    store = state.store
    publish_prepared(publication.OWNERS['playlists'], store.conn)
    assert _playlist_transfers(store, state.account)[1] == state.transfer
    playlist_gc_retry.clear(store, state.account, 'PLAYLIST')
    gc = publication.OWNERS['gc']
    gc.publish(store.conn, stage=stage(gc, store.conn))
    assert playlist_gc_retry.states(store) == {}
    assert gc.value(store.conn, state.old_gc, state.gc_key) == state.originals[state.gc_key]
