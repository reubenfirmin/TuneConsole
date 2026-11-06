"""Browser evidence must explain hidden forms without delaying usable ones."""
from playwright.sync_api import expect
import pytest

from tests.browser.test_audio_vibes_browser import DESCRIPTIONS, playback_route, visible_card


@pytest.fixture
def diagnostic_page(page, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    page.add_init_script('''(() => {
      window.chrome ||= {};
      chrome.runtime = {connect() {
        const messages = [], disconnects = [];
        const port = {onMessage: {addListener: callback => messages.push(callback)},
          onDisconnect: {addListener: callback => disconnects.push(callback)},
          postMessage() {}, disconnect() {}};
        // Installed 0.1.23 replies without a collection protocol. A local
        // disconnect does not call this side's onDisconnect listeners.
        setTimeout(() => messages.forEach(callback => callback({type: 'ready'})), 0);
        return port;
      }};
    })();''')
    page.route('**/bridge/status', lambda route: playback_route(route, {'position': 1, 'duration': 240}))
    records = []

    def record(route):
        records.append(route.request.post_data_json)
        # An old backend or unavailable log sink cannot block the human form.
        route.fulfill(status=503, json={'recorded': False})

    page.route('**/audio-vibes/diagnostics', record)
    return page, records


def test_legacy_handshake_diagnostics_do_not_block_zero_check_labels(diagnostic_page, live_app, store):
    page, records = diagnostic_page
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card).to_be_visible()
    expect(card.locator('[data-vibe-prediction]')).to_contain_text('0.1.24')
    choice = card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)
    expect(choice).to_be_enabled()
    # Repeated identical heartbeats do not emit a diagnostic per poll.
    page.evaluate('''() => {
      for (let count = 0; count < 12; count++)
        window.dispatchEvent(new CustomEvent('bridge-status', {detail: playbackStatus.snapshot}));
    }''')
    choice.click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    run_id = card.get_attribute('data-run-id')
    assert store.audio_vibes.feedback(run_id)['vibes'] == ['cruise']
    assert store.audio_vibes.feedback(run_id)['sample_ids'] == []
    started = [row for row in records if row['event'] == 'started']
    assert len(started) == 1
    assert all(started[0][key] for key in ('runtime_available', 'locks_available', 'secure_context'))
    assert started[0]['asset_version'] and started[0]['path'] == '/playlists'
    assert any(row['event'] == 'extension_legacy' and row['protocol'] == 'legacy' for row in records)
    assert any(row['event'] == 'cards' and row['card_visible'] and row['can_label']
               and row['run_id'] == run_id and row['samples'] == 0 for row in records)
    observed = [row for row in records if row['event'] == 'playback' and row['reason'] == 'observed']
    assert len(observed) == 1
    assert len(records) < 20
    assert all('title' not in row and 'artist' not in row and 'audio' not in row for row in records)


def test_actual_browser_diagnostics_are_accepted_by_backend(diagnostic_page, live_app, caplog):
    import logging

    page, _ = diagnostic_page
    page.unroute('**/audio-vibes/diagnostics')
    responses = []
    page.on('response', lambda response: responses.append(response)
            if response.url.endswith('/audio-vibes/diagnostics') else None)
    with caplog.at_level(logging.INFO, logger='yt_playlist.web.routes.audio_vibes'):
        with page.expect_response(lambda response: response.url.endswith('/audio-vibes/diagnostics')
                                  and response.request.post_data_json.get('can_label') is True):
            page.goto(live_app + '/playlists')
    assert responses and all(response.status == 200 for response in responses)
    assert any(record.message.startswith('Audio vibe browser:') and "'can_label': True" in record.message
               for record in caplog.records)
    expect(visible_card(page).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_be_enabled()


def test_diagnostics_explain_missing_web_locks(diagnostic_page, live_app):
    page, records = diagnostic_page
    page.add_init_script("Object.defineProperty(navigator, 'locks', {value: undefined});")
    with page.expect_response(lambda response: response.url.endswith('/audio-vibes/diagnostics')
                              and response.request.post_data_json.get('event') == 'card_blocked'):
        page.goto(live_app + '/playlists')
    assert any(row['event'] == 'started' and not row['locks_available'] for row in records)
    assert any(row['event'] == 'card_blocked' and row['reason'] == 'web_locks_unavailable' for row in records)
    expect(page.locator('[data-audio-vibe-banner]')).to_be_hidden()


def test_card_module_failure_still_reports_extension_and_playback(diagnostic_page, live_app):
    page, records = diagnostic_page
    page.route('**/static/audio-vibe-cards.js*', lambda route: route.fulfill(
        content_type='text/javascript', body="throw Error('Controlled card module failure');"))
    with page.expect_response(lambda response: response.url.endswith('/audio-vibes/diagnostics')
                              and response.request.post_data_json.get('event') == 'playback'
                              and response.request.post_data_json.get('reason') == 'observed'):
        page.goto(live_app + '/playlists')
    assert any(row['event'] == 'cards_failed' and row['detail'] == 'Controlled card module failure' for row in records)
    assert any(row['event'] == 'extension_legacy' for row in records)
    assert page.evaluate('!!JSON.parse(localStorage.getItem("tuneconsole.audio-vibes.run.1"))?.id')
