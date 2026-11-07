"""Connection-local revision counters may repeat after a real Store reopen."""
import pytest

from yt_playlist.core.db_rebuild import install
from yt_playlist.rec import candidate_refresh, rec_params


@pytest.mark.parametrize('internal', [False, True])
@pytest.mark.parametrize('kind', ['preparation', 'foreground'])
def test_reopen_retires_memo_results_before_serial_catches_up(file_backed_store, kind, internal):
    store, calls = file_backed_store, []
    def read():
        def calculate(reader):
            calls.append(True)
            return {'value': reader.settings.get_setting('memo-input')}
        if kind == 'foreground':
            with store.read_view(tracked=True) as reader:
                return reader.memoized_read('memo-value', lambda: calculate(reader))
        with store.read_snapshot() as snapshot:
            return snapshot.store.memoized_read('memo-value', lambda: calculate(snapshot.store))

    store.settings.set_setting('memo-input', 'before')
    first = read()
    first['value'] = 'detached caller edit'
    assert read() == {'value': 'before'}
    assert len(calls) == 1
    serial = store.conn.total_changes
    epoch = tuple(store.conn.execute('PRAGMA ' + field).fetchone()[0]
                  for field in ('data_version', 'schema_version'))
    store.settings.set_setting('memo-input', 'after')
    with store._lock:
        store.conn.close()
        install.reopen(store, internal=internal)
    assert tuple(store.conn.execute('PRAGMA ' + field).fetchone()[0]
                 for field in ('data_version', 'schema_version')) == epoch
    assert serial < 10000
    for progress in range(serial):
        if store.conn.total_changes >= serial:
            break
        store.settings.set_setting('unrelated-progress', str(progress))
    assert store.conn.total_changes >= serial
    assert read() == {'value': 'after'}
    assert read() == {'value': 'after'}
    assert len(calls) == 2


def test_reopened_foreground_preferences_use_current_consumed_parameter(file_backed_store):
    store, now = file_backed_store, 1000
    rec_params.set_param(store, 'modes_k', 14)
    with store.read_view(tracked=True) as reader:
        before = candidate_refresh.preferences(reader, now)
    serial = store.conn.total_changes
    rec_params.set_param(store, 'modes_k', 15)
    with store._lock:
        store.conn.close()
        install.reopen(store)
    assert serial < 10000
    for progress in range(serial):
        if store.conn.total_changes >= serial:
            break
        store.settings.set_setting('unrelated-progress', str(progress))
    with store.read_view() as reader:
        current = candidate_refresh.preferences(reader, now)
    assert current != before
    with store.read_view(tracked=True) as reader:
        assert rec_params.get_param(reader, 'modes_k') == 15
        assert candidate_refresh.preferences(reader, now) == current
