"""Repository ownership retains the model publisher's atomic transaction."""
import sqlite3
import struct

import pytest

from yt_playlist.rec import embed, model_refresh
from yt_playlist.repos.rec_model import VectorChanges


def test_publication_owners_require_an_outer_transaction(store):
    for publish in (
        lambda: store.rec_model.publish_training_generation({}),
        lambda: store.settings.publish_values({'generation': 'new'}),
        lambda: store.modes.publish_generation([], []),
        lambda: store.rec_refresh.acknowledge(1),
    ):
        with pytest.raises(RuntimeError, match='transaction'):
            publish()


def test_late_mode_failure_rolls_back_vectors_settings_and_queue(store, monkeypatch):
    old, new = struct.pack('f', 1), struct.pack('f', 2)
    store.rec_model.replace_rec_vectors([('song', old)])
    store.settings.set_setting('generation', 'old')
    store.rec_refresh.enqueue()
    revision = store.rec_refresh.revision()
    prepared = {'vectors': {'rec_vectors': [('song', new)]}, 'settings': {'generation': 'new'},
                'modes': ([(3, 'Mode', '[]', new, 1, '["song"]', 1, 1, 'space')], []), 'tracks': 1}
    monkeypatch.setattr(model_refresh, 'prepare', lambda *args, **kwargs: prepared)
    with store.conn:
        store.conn.execute("CREATE TRIGGER fail_mode BEFORE INSERT ON taste_modes "
                           "BEGIN SELECT RAISE(ABORT,'late publication failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='late publication failure'):
        model_refresh.rebuild(store, 10)
    assert not store.conn.in_transaction
    assert store.rec_model.get_rec_vectors() == [('song', old)]
    assert store.settings.get_setting('generation') == 'old'
    assert store.modes.list_modes() == []
    assert store.rec_refresh.revision() == revision


@pytest.mark.parametrize('space', ['settings', 'arbitrary_sql'])
def test_vector_publisher_rejects_other_owners_and_sql(store, space):
    store.settings.set_setting('preserved', 'yes')
    with store.read_snapshot() as snapshot, pytest.raises(ValueError, match='model table'):
        changes = {'rec_vectors': VectorChanges((('song', struct.pack('f', 1)),), ()),
                   space: VectorChanges((), ())}
        snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
    assert store.rec_model.get_rec_vectors() == []
    assert store.settings.get_setting('preserved') == 'yes'


def test_vector_changes_preserve_unchanged_rows_and_omit_unrequested_spaces(store):
    old, new = struct.pack('f', 1), struct.pack('f', 2)
    store.rec_model.replace_rec_vectors([('same', old), ('update', old), ('removed', old)])
    store.rec_model.replace_rec_artist_vectors([('artist', old)])
    with store.read_snapshot() as snapshot:
        changes = snapshot.store.rec_model.prepare_training_changes({
            'rec_vectors': [('same', old), ('update', new), ('added', new)]})
        assert changes == {'rec_vectors': VectorChanges((('update', new), ('added', new)), ('removed',))}
        assert snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
    assert dict(store.rec_model.get_rec_vectors()) == {'same': old, 'update': new, 'added': new}
    assert store.rec_model.get_rec_artist_vectors() == [('artist', old)]


def test_identical_vectors_do_not_write_or_invalidate_an_existing_snapshot(store):
    blob = struct.pack('f', 1)
    store.rec_model.replace_rec_vectors([('song', blob)])
    with store.read_snapshot() as previous, store.read_snapshot() as snapshot:
        assert previous.store.rec_model.get_rec_vectors() == [('song', blob)]
        changes = snapshot.store.rec_model.prepare_training_changes({'rec_vectors': [('song', blob)]})
        assert changes == {}
        before = store.conn.total_changes
        assert snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
        assert store.conn.total_changes == before
        assert previous.publish(lambda: None)


def test_empty_target_removes_a_space_and_invalidates_its_readers(store):
    store.rec_model.replace_rec_vectors([('song', struct.pack('f', 1))])
    with store.read_snapshot() as previous, store.read_snapshot() as snapshot:
        previous.store.rec_model.get_rec_vectors()
        changes = snapshot.store.rec_model.prepare_training_changes({'rec_vectors': []})
        assert changes == {'rec_vectors': VectorChanges((), ('song',))}
        assert snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
        assert not previous.publish(lambda: pytest.fail('missed changed vector space'))
    assert store.rec_model.get_rec_vectors() == []


def test_concurrent_model_write_invalidates_prepared_delta(store):
    old, new, latest = (struct.pack('f', n) for n in (1, 2, 3))
    store.rec_model.replace_rec_vectors([('song', old)])
    with store.read_snapshot() as snapshot:
        changes = snapshot.store.rec_model.prepare_training_changes({'rec_vectors': [('song', new)]})
        store.rec_model.replace_rec_vectors([('song', latest)])
        assert not snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
    assert store.rec_model.get_rec_vectors() == [('song', latest)]


def test_delta_preparation_requires_snapshot_and_rejects_duplicate_keys(store):
    with pytest.raises(RuntimeError, match='snapshot'):
        store.rec_model.prepare_training_changes({})
    with store.read_snapshot() as snapshot, pytest.raises(ValueError, match='Duplicate'):
        snapshot.store.rec_model.prepare_training_changes({'rec_vectors': [('song', b'a'), ('song', b'b')]})


@pytest.mark.parametrize('space', [
    '', '_content', '_discovered_content', '_artist', '_artist_content',
])
def test_delta_loads_and_tied_rankings_match_full_replacement(store, space):
    replace = getattr(store.rec_model, f'replace_rec{space}_vectors')
    load = getattr(store.rec_model, f'get_rec{space}_vectors')
    blob = struct.pack('ff', 1, 0)
    replace([('a', blob), ('c', blob)])
    target = [('a', blob), ('b', blob), ('c', blob)]
    with store.read_snapshot() as snapshot:
        changes = snapshot.store.rec_model.prepare_training_changes({f'rec{space}_vectors': target})
        assert snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
    rows = load()
    keys, matrix, _ = embed._load_vector_rows(rows)
    ranked = embed._rank(keys, matrix, matrix[0], set(), 3)
    replace(target)
    expected_keys, expected_matrix, _ = embed._load_vector_rows(load())
    assert rows == target
    assert ranked == embed._rank(expected_keys, expected_matrix, expected_matrix[0], set(), 3)


def test_discovery_order_only_change_preserves_the_encoder_order(store):
    blob = struct.pack('f', 1)
    store.rec_model.replace_rec_discovered_content_vectors([('b', blob), ('a', blob)])
    target = [('a', blob), ('b', blob)]
    with store.read_snapshot() as snapshot:
        changes = snapshot.store.rec_model.prepare_training_changes({'rec_discovered_content_vectors': target})
        assert changes == {'rec_discovered_content_vectors': VectorChanges(tuple(target), (), replace=True)}
        assert snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
    assert store.rec_model.get_rec_discovered_content_vectors() == target


def test_discovery_updates_and_appends_do_not_require_replacement(store):
    old, new = struct.pack('f', 1), struct.pack('f', 2)
    store.rec_model.replace_rec_discovered_content_vectors([('b', old), ('a', old)])
    target = [('b', new), ('a', old), ('c', new)]
    with store.read_snapshot() as snapshot:
        changes = snapshot.store.rec_model.prepare_training_changes({'rec_discovered_content_vectors': target})
        assert changes == {'rec_discovered_content_vectors': VectorChanges((('b', new), ('c', new)), ())}
        assert snapshot.publish(lambda: store.rec_model.publish_training_generation(changes))
    assert store.rec_model.get_rec_discovered_content_vectors() == target
