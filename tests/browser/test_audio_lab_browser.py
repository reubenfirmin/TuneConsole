"""Real MV3 tab capture of a generated reference; no Google/network/audio corpus."""
from pathlib import Path
import json
import re
import shutil

import pytest
from playwright.sync_api import expect

LAB = Path(__file__).resolve().parents[2] / 'experiments' / 'audio_lab'


def invoke_toolbar(context, worker, page):
    # Renderer keyboard input cannot invoke Chrome's extension action. Use the
    # browser's real action dispatch so tabCapture receives its gesture grant.
    session = context.browser.new_browser_cdp_session()
    try:
        tabs = session.send('Target.getTargets', {'filter': [{'type': 'tab'}, {'exclude': True}]})['targetInfos']
        targets = [tab['targetId'] for tab in tabs if tab['url'] == page.url]
        assert len(targets) == 1, tabs
        target = targets[0]
        session.send('Extensions.triggerAction', {'id': worker.url.split('/')[2], 'targetId': target})
    finally:
        session.detach()


def wait_for_cleanup(worker):
    assert worker.evaluate("""async () => {
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        if (!(await chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']})).length) return true;
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      return false;
    }""")


@pytest.fixture
def audio_lab(playwright, browser_type_launch_args, tmp_path, request):
    lab = LAB
    variant = getattr(request, 'param', None)
    if variant in {'legacy', 'tampered'}:
        # Installed-extension negative controls preserve the real source bundle.
        lab = tmp_path / (variant + '-lab')
        ignored = ['__pycache__'] + (['runtime.json', 'essentia-csp.js'] if variant == 'legacy' else [])
        shutil.copytree(LAB, lab, ignore=shutil.ignore_patterns(*ignored))
        if variant == 'tampered':
            path = lab / 'vendor/runtime.json'
            if not path.exists():
                pytest.skip('Build the optional CSP-compatible research runtime first')
            manifest = json.loads(path.read_text())
            manifest['files'][0]['sha256'] = '0' * 64
            path.write_text(json.dumps(manifest))
    context = playwright.chromium.launch_persistent_context(
        tmp_path / 'profile', **{**browser_type_launch_args, 'channel': 'chromium'},
        ignore_default_args=['--mute-audio'],
        args=[f'--disable-extensions-except={lab}', f'--load-extension={lab}',
              # Chromium's FakeAudioManager keeps rendering/capture alive while
              # never opening a hardware output. --mute-audio zeros tab capture.
              '--disable-audio-output',
              '--autoplay-policy=no-user-gesture-required', '--enable-unsafe-extension-debugging'],
    )
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        if variant == 'loading':
            context.add_init_script("""
              const fetchAssets = window.fetch;
              window.fetch = (...args) => {
                if (String(args[0]).includes('vendor/bundle.json')) {
                  window.modelAssetsPending = true;
                  return new Promise(() => {});
                }
                return fetchAssets(...args);
              };
            """)
        page = context.new_page()
        page.goto(worker.url.rsplit('/', 1)[0] + '/reference.html', wait_until='commit')
        if variant == 'loading':
            page.wait_for_function('() => window.modelAssetsPending === true')
        else:
            expect(page.locator('#model-status')).not_to_have_text('Checking for local model assets…')
        yield context, worker, page
    finally:
        context.close()


def test_generated_reference_capture_preserves_playback_and_cleans_up(audio_lab):
    context, worker, page = audio_lab
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_have_text('Analysis on. Playback remains audible.', timeout=10000)
    page.locator('audio').evaluate('(audio) => audio.play()')
    expect(page.locator('#results li')).to_have_count(1, timeout=20000)
    assert page.locator('audio').evaluate('(audio) => !audio.paused && audio.currentTime > 12')
    offscreen = worker.evaluate("chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']})")
    assert len(offscreen) == 1
    page.get_by_role('button', name='Stop analysis').click()
    expect(page.get_by_role('status')).to_have_text('Stopped')
    assert worker.evaluate("chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']})") == []
    assert page.locator('audio').evaluate('(audio) => !audio.paused')
    with page.expect_download() as download:
        page.get_by_role('button', name='Export measurements').click()
    payload = json.loads(Path(download.value.path()).read_text())
    result = payload['results'][0]
    assert result['descriptors']['rms'] == pytest.approx(.1 / 2**.5, abs=.01)
    assert result['raw_audio_retained'] is False
    assert result['intensity'] is None
    assert payload['reference']['synthetic'] is True
    assert payload['human_judgments'] is None


def test_capture_denied_until_permitted_and_navigation_stops_it(audio_lab):
    context, worker, page = audio_lab
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('confirm its source permission')
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    page.locator('audio').evaluate('(audio) => audio.play()')
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on')
    page.reload()
    wait_for_cleanup(worker)


@pytest.mark.parametrize('audio_lab', ['loading'], indirect=True)
def test_toolbar_permission_guidance_does_not_wait_for_optional_assets(audio_lab):
    context, worker, page = audio_lab
    expect(page.locator('#model-status')).to_have_text('Checking for local model assets…')
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('confirm its source permission')
    assert worker.evaluate("chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']})") == []
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on')
    page.get_by_role('button', name='Stop analysis').click()
    expect(page.get_by_role('status')).to_have_text('Stopped')
    wait_for_cleanup(worker)
    expect(page.locator('#model-status')).to_have_text('Checking for local model assets…')


@pytest.mark.parametrize('end', ['permission', 'tab_close'])
def test_reference_revocation_or_tab_close_releases_capture(audio_lab, end):
    context, worker, page = audio_lab
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on')
    page.locator('audio').evaluate('(audio) => audio.play()')
    if end == 'permission':
        page.locator('#permitted').uncheck()
        expect(page.get_by_role('status')).to_have_text('Stopped')
        assert page.locator('audio').evaluate('(audio) => !audio.paused')
    else:
        page.close()
    wait_for_cleanup(worker)
