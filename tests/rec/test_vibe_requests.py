"""Worker ownership, durable intent and publication races for explicit Vibes."""
from copy import deepcopy
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from tests.home_fixture_support import browser_store, publish_menu
from yt_playlist.core import activity
from yt_playlist.rec import home_cache, home_cards, home_vibe, mix_tuning
from yt_playlist.rec.vibe_requests import VibeRequests


@pytest.fixture
def setup(browser_store, monkeypatch):
    store = browser_store
    payload = publish_menu(store)
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1000., work=activity.Activity(),
                          logger=logging.getLogger(__name__), rec_worker=None)
    requests = VibeRequests(ctx)
    monkeypatch.setattr(home_vibe, '_access', lambda *_: True)

    def calculate(calculation, request):
        menu = deepcopy(payload['menus'][0])
        vibe = home_vibe.current(calculation.store, request.now)
        for proto in menu['protos']:
            proto['recipe']['vibe'] = vibe
            proto['name'] = vibe + ' worker mix'
        return home_cards.CardsResult(menu['protos'], menu['slots'], None, None,
                                      [], [], menu['epoch'], vibe)

    monkeypatch.setattr(home_cards.HomeCards, 'cards', calculate)
    yield store, requests, ctx, calculate
    assert ctx.work.wait(5), 'The accepted worker must finish before closing its fixture'


def wait_done(requests):
    assert requests.ctx.work.wait(5)
    return requests.state()


def test_generation_outlives_parent_deadline_without_holding_writer(setup, monkeypatch):
    store, requests, ctx, calculate = setup
    entered, release = threading.Event(), threading.Event()
    owner = threading.get_ident()
    elapsed = [0.]
    real_clock = time.monotonic
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: real_clock() + elapsed[0]))

    def delayed(calculation, request):
        assert threading.get_ident() != owner
        assert not store._lock.held_by_current_thread
        assert not store.conn.in_transaction
        entered.set()
        assert release.wait(5)
        # Advance beyond both the ten-second read bound and the parent's
        # deadline without adding a ten-second sleep to the suite.
        elapsed[0] = 11.
        activity.check_cancellable_work()
        assert calculation.store.settings.get_setting(home_vibe.SETTING) == 'relax'
        return calculate(calculation, request)

    monkeypatch.setattr(home_cards.HomeCards, 'cards', delayed)
    try:
        with activity.work_budget(10):
            accepted = requests.select('relax', 1000.)
            assert entered.wait(3)
        assert accepted['phase'] == 'pending'
        assert home_cache.read(store, 'home_tuned') == {}
    finally:
        release.set()
    assert wait_done(requests)['phase'] == 'ready'
    menu = home_cache.read(store, 'home_tuned')['menus'][0]
    assert {p['recipe']['vibe'] for p in menu['protos']} == {'relax'}


def test_superseded_completion_never_publishes_or_runs_parallel_calculations(setup, monkeypatch):
    store, requests, ctx, calculate = setup
    first, second, release_first, release_second = (threading.Event() for _ in range(4))
    active = []

    def delayed(calculation, request):
        value = home_vibe.current(calculation.store, request.now)
        assert not active
        active.append(value)
        try:
            (first if value == 'relax' else second).set()
            assert (release_first if value == 'relax' else release_second).wait(5)
            return calculate(calculation, request)
        finally:
            active.pop()

    monkeypatch.setattr(home_cards.HomeCards, 'cards', delayed)
    try:
        old = requests.select('relax', 1000.)
        assert first.wait(3)
        latest = requests.select('energize', 1001.)
        assert latest['id'] != old['id']
        release_first.set()
        assert second.wait(3)
        assert home_cache.read(store, 'home_tuned') == {}
        assert requests.state()['id'] == latest['id']
    finally:
        release_first.set()
        release_second.set()
    assert wait_done(requests) == {**latest, 'phase': 'ready'}
    assert {p['recipe']['vibe'] for p in home_cache.read(store, 'home_tuned')['menus'][0]['protos']} == {'energize'}


def test_reset_during_generation_invalidates_accepted_request(setup, monkeypatch):
    store, requests, ctx, calculate = setup
    entered, release = threading.Event(), threading.Event()

    def delayed(calculation, request):
        entered.set()
        assert release.wait(5)
        return calculate(calculation, request)

    monkeypatch.setattr(home_cards.HomeCards, 'cards', delayed)
    try:
        requests.select('relax', 1000.)
        assert entered.wait(3)
        mix_tuning.reset(store)
    finally:
        release.set()
    assert wait_done(requests) is None
    assert home_cache.read(store, 'home_tuned') == {}


def test_restart_resumes_persisted_selection_without_another_mutation(setup, monkeypatch):
    store, requests, ctx, _ = setup
    with monkeypatch.context() as paused:
        paused.setattr(requests, 'resume', lambda: None)
        accepted = requests.select('release', 1000.)
    restarted = VibeRequests(ctx)
    restarted.resume()
    assert wait_done(restarted) == {**accepted, 'phase': 'ready'}
    assert store.settings.get_setting(mix_tuning.VIBE_UPDATED_AT_SETTING) == '1000.0'


@pytest.mark.parametrize('count', [2, 3])
def test_incomplete_preparation_preserves_complete_board_then_can_resume(setup, monkeypatch, count):
    store, requests, ctx, calculate = setup
    baseline = home_cache.read(store, 'home_prepared')

    def incomplete(calculation, request):
        result = calculate(calculation, request)
        result.protos = result.protos[:count]
        result.slots = result.slots[:count]
        return result

    monkeypatch.setattr(home_cards.HomeCards, 'cards', incomplete)
    accepted = requests.select('relax', 1000.)
    assert wait_done(requests) == {**accepted, 'phase': 'waiting'}
    assert home_cache.read(store, 'home_prepared') == baseline
    assert home_cache.read(store, 'home_tuned') == {}
    monkeypatch.setattr(home_cards.HomeCards, 'cards', calculate)
    # A restarted worker has no backoff from the previous process.
    resumed = VibeRequests(ctx)
    resumed.resume()
    assert wait_done(resumed) == {**accepted, 'phase': 'ready'}


def test_failure_is_terminal_for_observation_and_keeps_previous_files(setup, monkeypatch):
    store, requests, ctx, _ = setup
    baseline = home_cache.read(store, 'home_prepared')

    def fail(*_):
        raise ValueError('controlled generation failure')

    monkeypatch.setattr(home_cards.HomeCards, 'cards', fail)
    accepted = requests.select('relax', 1000.)
    assert wait_done(requests) == {**accepted, 'phase': 'failed'}
    requests.resume()
    assert not ctx.work.active
    assert home_cache.read(store, 'home_prepared') == baseline


