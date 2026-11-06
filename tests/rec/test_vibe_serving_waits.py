"""No-op serving work must not queue behind an unrelated maintenance writer."""
from contextlib import contextmanager
import threading
import time

import pytest

from yt_playlist.rec import group_roots, mix_tuning


@contextmanager
def busy_writer(store):
    entered, release = threading.Event(), threading.Event()

    def hold():
        with store._lock:
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(1)
    watchdog = threading.Timer(3, release.set)
    watchdog.start()
    try:
        yield
    finally:
        release.set()
        watchdog.cancel()
        watchdog.join(1)
        worker.join(1)
        assert not worker.is_alive()


@pytest.mark.parametrize('active', [False, True], ids=['unset', 'unexpired'])
def test_unexpired_tuning_does_not_wait_for_the_maintenance_writer(file_backed_store, active):
    store = file_backed_store
    if active:
        store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, '1000')
        store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, '1000')
        store.settings.set_setting('home_mix_vibe', 'relax')
    changes = store.conn.total_changes
    with busy_writer(store):
        started = time.monotonic()
        assert mix_tuning.expire(store, 1001) is False
        assert time.monotonic() - started < 1, 'Unexpired tuning waited for the writer'
    assert store.conn.total_changes == changes
    assert store.settings.get_setting('home_mix_vibe') == ('relax' if active else None)


def test_empty_capture_needs_no_writer_or_retained_lifetime(file_backed_store):
    store = file_backed_store
    assert store._compact_group_files
    changes = store.conn.total_changes
    with busy_writer(store):
        started = time.monotonic()
        assert group_roots.pending(store, {}, 1000) is None
        assert time.monotonic() - started < 1, 'An empty capture waited for the writer'
    assert store.conn.total_changes == changes


def test_expiry_rechecks_a_new_adjustment_after_its_read_view(file_backed_store, monkeypatch):
    store = file_backed_store
    now = 1000 + mix_tuning.TTL_S
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, '1000')
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, '1000')
    store.settings.set_setting('home_mix_vibe', 'relax')
    entered, observed, release = threading.Event(), threading.Event(), threading.Event()
    original = store.read_view
    observations, errors = [], []

    @contextmanager
    def observe(**kwargs):
        with original(**kwargs) as reader:
            observations.append(reader.settings.get_setting(mix_tuning.VIBE_UPDATED_AT_SETTING))
            yield reader
        observed.set()

    monkeypatch.setattr(store, 'read_view', observe)

    def adjust():
        try:
            with store._lock:
                entered.set()
                assert observed.wait(3), 'Expiry never inspected committed tuning'
                with mix_tuning.adjusting_vibe(store, now):
                    store.settings.set_setting('home_mix_vibe', 'concentrate')
                release.set()
        except BaseException as error:
            errors.append(error)
            release.set()

    worker = threading.Thread(target=adjust)
    worker.start()
    try:
        assert entered.wait(1)
        assert mix_tuning.expire(store, now) is False
        assert release.wait(1)
    finally:
        observed.set()
        worker.join(4)
    assert not worker.is_alive() and not errors
    assert observations == ['1000']
    assert store.settings.get_setting('home_mix_vibe') == 'concentrate'
    assert store.settings.get_setting(mix_tuning.VIBE_UPDATED_AT_SETTING) == str(now)
