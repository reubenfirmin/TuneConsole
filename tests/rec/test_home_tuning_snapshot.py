"""A Home tuning identity describes one coherent set of user preferences."""
from yt_playlist.rec import candidate_refresh, home_prepared, mix_tuning, rec_params


def test_tuning_key_pins_all_inputs_before_a_concurrent_preference_update(store, monkeypatch):
    before = home_prepared.tuning_key(store, 1000)
    preferences = candidate_refresh.preferences
    readers = []
    make_reader = store._make_reader

    def opened():
        reader = make_reader()
        readers.append(reader)
        return reader

    def changed(reader, now, **kwargs):
        result = preferences(reader, now, **kwargs)
        # This later field must remain part of the already pinned tuning state.
        # The next calculation must see it; the in-flight calculation must not.
        rec_params.set_param(store, 'breadth_bias', '.75')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, '_make_reader', opened)
        patch.setattr(candidate_refresh, 'preferences', changed)
        assert home_prepared.tuning_key(store, 1000) == before
    assert len(readers) == 1
    assert home_prepared.tuning_key(store, 1000) != before


def test_tuning_key_preserves_an_owned_uncommitted_preference_change(store):
    before = home_prepared.tuning_key(store, 1000)
    with store._lock:
        store.conn.execute('BEGIN')
        try:
            store.conn.execute("INSERT INTO settings(key,value) VALUES ('rec_param:breadth_bias','.75')")
            assert home_prepared.tuning_key(store, 1000) != before
            assert store.conn.in_transaction
        finally:
            store.conn.rollback()
    assert home_prepared.tuning_key(store, 1000) == before


def test_tuning_key_expires_temporary_controls_before_capturing_their_inputs(store):
    baseline = home_prepared.tuning_key(store, 1000)
    with mix_tuning.adjusting(store, 1000):
        rec_params.set_param(store, 'breadth_bias', '.75')
    adjusted = home_prepared.tuning_key(store, 1001)
    assert adjusted != baseline
    expired = home_prepared.tuning_key(store, 1000 + mix_tuning.TTL_S)
    assert expired != adjusted
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None
    assert rec_params.get_param(store, 'breadth_bias') == 0
