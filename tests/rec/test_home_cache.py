"""Home cache files precede a short SQL publication, with atomic selection heads."""
from pathlib import Path

import pytest

from yt_playlist.core import fao, home_outputs, recommendation_outputs as files
from yt_playlist.rec import home_cache
from tests.core.test_predecessor_writes import installed


def test_selection_round_trip_keeps_file_work_outside_repo_and_sql(store, monkeypatch):
    write, read = fao.write, files.read
    observed = []

    def written(path, data):
        assert not store.conn.in_transaction
        assert not store._lock.held_by_current_thread
        observed.append(path)
        return write(path, data)

    def restored(*args, **kwargs):
        assert not store._lock.held_by_current_thread
        return read(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('Home cache used the legacy durable publisher')

    monkeypatch.setattr(fao, 'write', written)
    monkeypatch.setattr(files, 'read', restored)
    monkeypatch.setattr(files, 'publish', forbidden)
    tailored = {'schema': 1, 'menus': [{'protos': [{'tracks': [{'title': 'été 東京'}]}]}]}
    held = {'menu': tailored['menus'][0], 'epoch': 2}
    with monkeypatch.context() as patch:
        patch.setattr(fao.os, 'fsync', forbidden)
        home_cache.remember(store, tailored, held, 1000)
    assert len(observed) == 2
    assert home_cache.read(store, 'home_tuned') == tailored
    assert home_cache.read(store, 'home_choice') == held
    assert not store.conn.in_transaction
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('failure', ['file', 'sql'])
def test_failed_selection_preserves_both_previous_heads(store, monkeypatch, failure):
    home_cache.remember(store, {'old': 'tailored'}, {'old': 'held'}, 1)
    original = fao.write if failure == 'file' else store.home_cache.publish
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise OSError('second document failed')
        return original(*args, **kwargs)

    if failure == 'file':
        monkeypatch.setattr(fao, 'write', interrupted)
    else:
        monkeypatch.setattr(store.home_cache, 'publish', interrupted)
    with pytest.raises(OSError, match='second document failed'):
        home_cache.remember(store, {'new': 'tailored'}, {'new': 'held'}, 2)
    assert home_cache.read(store, 'home_tuned') == {'old': 'tailored'}
    assert home_cache.read(store, 'home_choice') == {'old': 'held'}
    assert not store.conn.in_transaction


def test_orphan_collection_cannot_remove_files_before_their_sql_commit(file_backed_store):
    store = file_backed_store
    with home_cache.writing(store):
        publication = home_cache.prepare(store, 'home_prepared', '{"tracks":[]}', 1)
        collector = files.OrphanCollector(home_cache.root(store))
        try:
            progress = collector.step(store.conn)
            assert progress.blocked and progress.files == 0
        finally:
            collector.close()
        assert all(Path(row[2]).is_file() for row in publication.filenames)
        from yt_playlist.core.db_writes import atomic_write
        with atomic_write(store):
            home_cache.publish(store, publication)
    assert files.collect_orphans(store.conn) == 0
    assert home_cache.read(store, 'home_prepared') == {'tracks': []}


def test_reader_keeps_its_manifest_when_a_new_head_is_published(file_backed_store):
    store = file_backed_store
    home_cache.put(store, 'home_prepared', {'menu': 'previous'}, 1)
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert home_cache.read(reader, 'home_prepared') == {'menu': 'previous'}
        home_cache.put(store, 'home_prepared', {'menu': 'replacement'}, 2)
        assert home_cache.read(reader, 'home_prepared') == {'menu': 'previous'}
    assert home_cache.read(store, 'home_prepared') == {'menu': 'replacement'}


def test_legacy_written_home_files_remain_readable_and_can_be_replaced(store):
    store.rec_proposals.put_proposals('home_prepared', {'menu': 'installed'}, 1)
    assert home_cache.read(store, 'home_prepared') == {'menu': 'installed'}
    home_cache.put(store, 'home_prepared', {'menu': 'replacement'}, 2)
    # Other installed consumers can also read the simple writer's format.
    assert store.rec_proposals.get_proposals('home_prepared') == {'menu': 'replacement'}


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_unavailable_cache_is_a_miss_and_can_be_rewritten(store, damage):
    home_cache.put(store, 'home_tuned', {'menu': 'useful'}, 1)
    _, manifest = store.home_cache.document('home_tuned')
    path = Path(manifest[2][0][1])
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'damaged')
    assert home_cache.read(store, 'home_tuned') is None
    home_cache.put(store, 'home_tuned', {'menu': 'useful'}, 2)
    assert home_cache.read(store, 'home_tuned') == {'menu': 'useful'}


def test_response_reuse_returns_detached_values_and_reopens_on_next_request(file_backed_store, monkeypatch):
    store = file_backed_store
    home_cache.put(store, 'home_prepared', {'tracks': ['original']}, 1)
    read, calls = home_outputs.read_manifest, []

    def counted(*args):
        calls.append(True)
        return read(*args)

    monkeypatch.setattr(home_outputs, 'read_manifest', counted)
    with store.read_view() as reader, home_cache.verified_reads(reader):
        home_cache.read(reader, 'home_prepared')['tracks'].append('changed')
        assert home_cache.read(reader, 'home_prepared') == {'tracks': ['original']}
    assert len(calls) == 1
    with store.read_view() as reader, home_cache.verified_reads(reader):
        assert home_cache.read(reader, 'home_prepared') == {'tracks': ['original']}
    assert len(calls) == 2


def test_publication_resolves_artifact_context_before_sql_and_retains_actual_roots(store, monkeypatch):
    from tests.core.test_home_publication import real_roots
    from yt_playlist.controllers import group_evidence

    real_roots(store)
    group = store.conn.execute('SELECT external_key FROM group_generations LIMIT 1').fetchone()[0]
    generation = store.conn.execute('SELECT generation_id FROM recommendation_generation_refs LIMIT 1').fetchone()[0]
    store.rec_ledger.evidence().put('artifact', 'home-context', {'group_generation': group})
    resolve = group_evidence.resolve
    resolved = []

    def outside_sql(*args, **kwargs):
        assert not store.conn.in_transaction and not store._lock.held_by_current_thread
        resolved.append(True)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(group_evidence, 'resolve', outside_sql)
    value = {'context': {'artifact': 'home-context'}, 'menus': [{'protos': [{'tracks': [
        {'serving_context': {'recommendation_generation_id': generation}}]}]}]}
    home_cache.put(store, 'home_prepared', value, 2)
    assert resolved == [True]
    assert store.conn.execute('SELECT 1 FROM home_display_group_roots').fetchone()
    assert store.conn.execute('SELECT 1 FROM home_display_recommendation_roots WHERE generation_id=?',
                              (generation,)).fetchone()
    assert home_cache.read(store, 'home_prepared') == value


@pytest.mark.parametrize('installed', [79, 90], indirect=True)
def test_prepared_legacy_selection_and_continuing_writes_keep_working(installed):
    from tests.core.test_home_publication import ready, real_roots, seed
    from yt_playlist.core.preparation import home_publication

    store, _ = installed
    seed(store)
    real_roots(store)
    home_publication.OWNER.publish(store.conn, stage=ready(store.conn))
    assert home_cache.read(store, 'home_prepared') == store.rec_proposals.get_proposals('home_prepared')
    assert home_cache.read(store, 'home_tuned') is None
    home_cache.put(store, 'home_prepared', {'menu': 'continuing write'}, 2000)
    assert home_publication.OWNER.selected(store.conn) is None
    assert home_cache.read(store, 'home_prepared') == {'menu': 'continuing write'}
