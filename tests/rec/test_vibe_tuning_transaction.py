"""A selected Vibe, its genre hold and its deadline become durable together."""
import pytest

from yt_playlist.rec import mix_tuning


def tuning_state(store):
    return (store.settings.get_setting('home_mix_vibe'),
            store.settings.get_setting(mix_tuning.VIBE_UPDATED_AT_SETTING),
            store.rec_proposals.get_proposals('home_genre_request'),
            store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING))


def seed(store, clearing):
    request = {'epoch': mix_tuning.card_epoch(store), 'include_genres': ['jazz']}
    if clearing:
        request['held_by_vibe'] = True
        store.settings.set_setting('home_mix_vibe', 'cruise')
        store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, '1000')
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, '1000')
    store.rec_proposals.put_proposals('home_genre_request', request, 1000)
    return tuning_state(store)


def choose(store, clearing):
    if clearing:
        store.settings.delete_setting('home_mix_vibe')
    else:
        store.settings.set_setting('home_mix_vibe', 'relax')


@pytest.mark.parametrize('clearing', [False, True], ids=['choose', 'clear'])
def test_another_reader_never_sees_a_preset_with_the_previous_deadline(
        file_backed_store, clearing):
    store = file_backed_store
    before = seed(store, clearing)
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        with mix_tuning.adjusting_vibe(store, 1100):
            choose(store, clearing)
            with store.read_view() as reader:
                assert tuning_state(reader) == before, 'A partially saved selection escaped'
    finally:
        store.conn.set_trace_callback(None)
    with store.read_view() as reader:
        preset, deadline, genres, ordinary_deadline = tuning_state(reader)
    assert preset == (None if clearing else 'relax')
    assert deadline == (None if clearing else '1100')
    assert bool(genres.get('held_by_vibe')) is not clearing
    assert genres['include_genres'] == ['jazz']
    assert genres['epoch'] == mix_tuning.card_epoch(store)
    assert ordinary_deadline == before[3]
    assert sum(sql == 'COMMIT' for sql in statements) == 1
    assert not store.conn.in_transaction


@pytest.mark.parametrize('clearing', [False, True], ids=['choose', 'clear'])
def test_a_failed_deadline_write_rolls_back_the_preset_and_genre_transition(
        file_backed_store, monkeypatch, clearing):
    store = file_backed_store
    before = seed(store, clearing)
    operation = 'delete_setting' if clearing else 'set_setting'
    original = getattr(store.settings, operation)

    def fail_deadline(key, *args):
        if key == mix_tuning.VIBE_UPDATED_AT_SETTING:
            raise RuntimeError('deadline write failed')
        return original(key, *args)

    monkeypatch.setattr(store.settings, operation, fail_deadline)
    with pytest.raises(RuntimeError, match='deadline write failed'):
        with mix_tuning.adjusting_vibe(store, 1100):
            choose(store, clearing)
    assert not store.conn.in_transaction
    with store.read_view() as reader:
        assert tuning_state(reader) == before
    # A failed adjustment must release the writer for a later deliberate retry.
    monkeypatch.setattr(store.settings, operation, original)
    with mix_tuning.adjusting_vibe(store, 1200):
        choose(store, clearing)
    with store.read_view() as reader:
        assert reader.settings.get_setting('home_mix_vibe') == (None if clearing else 'relax')
