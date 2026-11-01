"""A lost Radio receipt keeps its exact selection and create guard on reload."""
import pytest
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui


@pytest.mark.parametrize('wait_for_deadline', [False, True])
def test_uncertain_seed_survives_reload(page, radio_ui, wait_for_deadline):
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.clock.install()
    selected = radio_ui.tracks[1]
    page.locator('.radio-seed-start').nth(1).click()
    expect(page.locator('.radio-launch')).to_be_visible()
    assert len(radio_ui.starts) == 1
    if wait_for_deadline:
        radio_ui.starts[0].abort('connectionreset')
        expect(page.locator('.radio-launch-recovery')).to_be_visible()
        page.clock.fast_forward(30_100)
    # Cover reload while the POST is still in flight as well as after its
    # missing receipt and bounded status check have become explicit.
    page.reload()
    recovery = page.locator('.radio-launch-recovery')
    expect(recovery).to_be_visible()
    expect(recovery.get_by_role('heading', name=selected['title'], exact=True)).to_be_visible()
    expect(recovery.get_by_text(selected['artist'], exact=True)).to_be_visible()
    expect(recovery.get_by_role('button', name='Check playback', exact=True)).to_be_visible()
    expect(page.locator('.radio-seed-start').first).to_be_disabled()
    assert len(radio_ui.starts) == 1
    recovery.get_by_role('button', name='Check playback', exact=True).click()
    page.reload()
    expect(recovery).to_be_visible()
    expect(page.locator('.radio-seed-start').first).to_be_disabled()
    assert len(radio_ui.starts) == 1
