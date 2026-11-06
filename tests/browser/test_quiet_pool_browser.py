"""Fresh failures leave the desktop menu and chosen genres available."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.home_fixture_support import browser_store as store
from tests.live_server import LiveServer
from tests.quiet_pool_support import DIAGNOSTIC, quiet_pool

pytestmark = pytest.mark.browser


@pytest.fixture
def quiet_pool_app(quiet_pool):
    case = quiet_pool
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        case.base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(case.http.app, host='127.0.0.1',
            port=listener.getsockname()[1], log_level='warning'))
        with LiveServer(server, sockets=[listener]):
            try:
                yield case
            finally:
                case.release.set()


@pytest.mark.parametrize('kind', ['json', 'value'])
@pytest.mark.parametrize('boundary', ['populate', 'prepare'])
def test_fresh_worker_failure_preserves_genres_menu_and_explicit_recovery(
        quiet_pool_app, page, kind, boundary, action_evidence):
    case = quiet_pool_app
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(case.base + '/')
    page.locator('#fingerprint .fp-collapse').click()
    panel = page.locator('#fingerprint')
    panel.get_by_role('tab', name='Genre focus', exact=True).click()
    panel.get_by_role('checkbox', name='jazz', exact=True).check()
    with page.expect_response('**/home/mix-genres'):
        panel.get_by_role('button', name='Make mixes', exact=True).click()
    board, card = page.locator('#home-mode-cards'), page.locator('#gen-fresh')
    expect(board).to_have_attribute('data-requested-genres', '["jazz"]')
    titles = board.locator('.gen-title').all_text_contents()
    assert titles
    case.fault, case.boundary = kind, boundary
    case.release.clear()
    try:
        card.get_by_role('button', name='Find fresh songs', exact=True).click()
        assert case.entered.wait(3)
        expect(card).to_have_attribute('aria-busy', 'true')
        assert board.locator('.gen-title').all_text_contents() == titles
        assert_quiet_action(page)
    finally:
        case.release.set()
    expect(card).to_have_attribute('aria-busy', 'false')
    expect(card.get_by_role('status')).to_have_text('Fresh mix not completed. You can try again.')
    expect(card.get_by_role('button', name='Find fresh songs', exact=True)).to_be_enabled()
    assert len(case.calls) == 1 and not case.refreshes
    job = case.ctx.jobs.latest('fresh_pool', {'include_genres': ['jazz'], 'avoid_genres': []})
    assert job.done and DIAGNOSTIC in job.error and job.validation_code is None
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-fresh-' + boundary + '-' + kind + '.png')
    page.reload()
    expect(card).to_have_attribute('aria-busy', 'false')
    expect(card.get_by_role('status')).to_have_text('Fresh mix not completed. You can try again.')
    expect(board).to_have_attribute('data-requested-genres', '["jazz"]')
    assert board.locator('.gen-title').all_text_contents() == titles and len(case.calls) == 1
    assert not card.get_attribute('hx-trigger')
    case.fault = None
    with page.expect_response('**/home/fresh-pool') as started:
        card.get_by_role('button', name='Find fresh songs', exact=True).click()
    assert started.value.ok
    assert case.ctx.work.wait(10), 'The explicit recovery must finish its real worker'
    expect(card).to_have_attribute('aria-busy', 'false')
    expect(card.get_by_role('status')).to_contain_text('Not enough matching songs yet')
    assert len(case.calls) == 2 and len(case.refreshes) == 1
    recovered = case.ctx.jobs.latest('fresh_pool', {'include_genres': ['jazz'], 'avoid_genres': []})
    assert recovered.id != job.id and recovered.done and not recovered.error
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-fresh-explicit-recovery.png')
