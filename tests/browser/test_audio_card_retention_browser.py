"""Real card module/template, with controlled playback and real persisted answers."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.browser.test_audio_vibes_browser import (
    CARDS, RUN, DESCRIPTIONS, seed_samples, vibes_enabled, visible_card,
)
from tests.test_audio_vibes import sample


def open_cards(page, app):
    # Own the observer in this harness so its maps can be inspected. Load the
    # unmodified production class, inspector, template and CSS. Separate tests
    # exercise the normal collection entry point and extension transport.
    page.route('**/audio-vibe-collection.js*', lambda route: route.fulfill(
        content_type='text/javascript', body='export {};'))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': False}))
    page.goto(app + '/playlists')
    page.evaluate('''async () => {
      const root = document.querySelector('[data-audio-vibe-banner]');
      const {createCards} = await import('/static/audio-vibe-banner.js');
      window.retentionCards = await createCards({root, taxonomy: root.dataset.taxonomyVersion,
        read(key, fallback = null) {return JSON.parse(localStorage.getItem(key)) ?? fallback;},
        write(key, value) {try {localStorage.setItem(key, JSON.stringify(value)); return true;}
          catch (error) {return false;}},
        async request(url, body) {
          const response = await fetch(url, body ? {method: 'POST',
            headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)} : {});
          if (!response.ok) {const error = Error('HTTP ' + response.status); error.status = response.status; throw error;}
          return response.json();
        }});
    }''')


def admit(page, playback):
    page.evaluate('''async playback => {
      await retentionCards.accept({run_id: playback.id, playback, samples: [], prediction: null,
        taxonomy_version: retentionCards.taxonomy,
        choices: JSON.parse(retentionCards.root.dataset.choices)}, {placeholder: true});
      await retentionCards.accept(await retentionCards.request('/audio-vibes/runs/' + playback.id));
    }''', playback)


def completed(store, count):
    playbacks = []
    for index in range(count):
        values = seed_samples(store, video_id=f'retention-{index}', title=f'Completed song {index}')
        first = values[0]
        store.audio_vibes.save_feedback(first['run_id'], [row['id'] for row in values],
                                        ['cruise'], 'audio-session-targets-1', None, index + 10)
        playbacks.append({'id': first['run_id'], **{
            key: first[key] for key in ('video_id', 'title', 'artist')}})
    return playbacks


def add_retired_history(page, playbacks):
    # Also exercise maintenance of an existing pre-upgrade index. Every compact
    # row represents a real saved server answer, not a manufactured dirty flag.
    page.evaluate('''async playbacks => {
      for (const playback of playbacks) await retentionCards.update(playback.id, {
        dismissed: true, draft: ['cruise'], dirty: false, playback, sample_ids: []});
      retentionCards.render();
    }''', playbacks)


def assert_released(page, run_id):
    page.wait_for_function('''id => {
      const cards = retentionCards;
      return !cards.cards.has(id) && !cards.restored.has(id) && !cards.loads.has(id)
        && !cards.root.querySelector(`[data-run-id="${id}"]`);
    }''', arg=run_id)


def test_long_history_keeps_only_live_payloads_and_pending_original_evidence(
        vibes_enabled, live_app, page, store, browser, record_property):
    playbacks = completed(store, 160)
    unanswered = seed_samples(store, count=2, video_id='unanswered', title='Still waiting for your labels')
    hidden = seed_samples(store, count=2, video_id='hidden-unanswered', title='Dismissed without an answer')
    pending = seed_samples(store, count=2, video_id='offline-answer', title='Unsynced human answer')
    open_cards(page, live_app)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for values in (unanswered, hidden, pending):
        first = values[0]
        admit(page, {'id': first['run_id'], **{key: first[key] for key in ('video_id', 'title', 'artist')}})
    hidden_id, pending_id = hidden[0]['run_id'], pending[0]['run_id']
    page.evaluate('''async id => {
      const cards = retentionCards, card = cards.cards.get(id);
      await cards.clicked({target: card.find('dismiss')});
    }''', hidden_id)
    page.route('**/audio-vibes/feedback', lambda route: route.fulfill(status=503, json={}))
    page.evaluate('''async id => {
      const cards = retentionCards, card = cards.cards.get(id);
      await cards.clicked({target: card.node.querySelector('[data-vibe-choice="relax"]')});
      await cards.writes.get(id);
      await cards.flush();
      await cards.clicked({target: card.find('dismiss')});
    }''', pending_id)
    page.wait_for_function('!retentionCards.saving')
    assert_released(page, hidden_id)
    assert_released(page, pending_id)
    # Pin a dismissed occurrence older than the bounded history: its repeated
    # heartbeat must remain suppressed, including after document reload.
    page.evaluate('([key, playback]) => localStorage.setItem(key, JSON.stringify(playback))', [RUN, playbacks[0]])
    result = page.evaluate('''async playbacks => {
      const cards = retentionCards;
      let maxLive = 0, maxAfterDismiss = 0, renderVisits = 0;
      const render = cards.render.bind(cards);
      cards.render = () => {renderVisits += cards.cards.size; return render();};
      for (const playback of playbacks) {
        await cards.accept({run_id: playback.id, playback, samples: [], prediction: null,
          taxonomy_version: cards.taxonomy, choices: JSON.parse(cards.root.dataset.choices)}, {placeholder: true});
        await cards.accept(await cards.request('/audio-vibes/runs/' + playback.id));
        maxLive = Math.max(maxLive, cards.cards.size);
        if (playback.id === playbacks[0].id) {
          await cards.clicked({target: cards.cards.get(playback.id).find('inspect')});
          if (!cards.find('attributes').children.length) throw Error('Inspector did not render');
        }
        await cards.clicked({target: cards.cards.get(playback.id).find('dismiss')});
        if (cards.find('diagnostics').open || cards.find('attributes').children.length)
          throw Error('Dismissed inspector retained');
        maxAfterDismiss = Math.max(maxAfterDismiss, cards.cards.size);
        if (cards.cards.has(playback.id) || cards.restored.has(playback.id) || cards.loads.has(playback.id))
          throw Error('Dismissed payload retained: ' + playback.id);
      }
      for (let i = 0; i < 50; i++) {cards.tick(); cards.render();}
      await cards.compactHistory();
      return {maxLive, maxAfterDismiss, renderVisits, records: cards.records(),
        cards: cards.cards.size, restored: cards.restored.size, loads: cards.loads.size,
        nodes: cards.root.querySelectorAll('[data-vibe-card]').length};
    }''', playbacks)
    assert result['maxLive'] == 2 and result['maxAfterDismiss'] == 1
    assert result['cards'] == result['restored'] == result['nodes'] == 1
    assert result['loads'] == 0
    assert result['renderVisits'] < 160 * 12
    records = {row['id']: row for row in result.pop('records')}
    assert len(records) == 68  # 64 recent tombstones, pinned current, three pending/unanswered.
    assert set(records[playbacks[0]['id']]) == {'id', 'dismissed', 'draft', 'tombstone'}
    assert records[hidden_id]['draft'] is None and records[hidden_id]['dismissed']
    assert records[pending_id]['dirty'] and records[pending_id]['dismissed']
    assert records[pending_id]['sample_ids'] == [row['id'] for row in pending]
    assert store.audio_vibes.feedback(pending_id) is None
    for width, height in ((1280, 900), (1920, 1080)):
        page.set_viewport_size({'width': width, 'height': height})
        expect(visible_card(page).locator('[data-vibe-track]')).to_have_text(unanswered[0]['title'])
        page.screenshot(path=f'/tmp/tuneconsole-886-retained-{width}.png')
    # Establish the exact served class, rather than assuming an edited file was loaded.
    served = page.request.get(live_app + '/static/audio-vibe-cards.js').body()
    source = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static/audio-vibe-cards.js'
    assert hashlib.sha256(served).hexdigest() == hashlib.sha256(source.read_bytes()).hexdigest()
    record_property('retention', json.dumps({**result, 'browser': browser.version,
        'occurrences': 160, 'class_sha256': hashlib.sha256(served).hexdigest()}))
    open_cards(page, live_app)
    expect(visible_card(page).locator('[data-vibe-track]')).to_have_text(unanswered[0]['title'])
    page.evaluate('''async playback => {
      await retentionCards.accept({run_id: playback.id, playback, samples: [], prediction: null,
        taxonomy_version: retentionCards.taxonomy,
        choices: JSON.parse(retentionCards.root.dataset.choices)}, {placeholder: true});
    }''', playbacks[0])
    assert_released(page, playbacks[0]['id'])
    retained = page.evaluate('key => JSON.parse(localStorage.getItem(key))', CARDS)
    by_id = {row['id']: row for row in retained}
    assert by_id[pending_id]['sample_ids'] == [row['id'] for row in pending]
    assert by_id[hidden_id]['draft'] is None
    assert not errors


def test_cross_tab_dismissal_and_pruning_fences_an_older_load(
        vibes_enabled, live_app, page, store):
    playbacks = completed(store, 80)
    first = playbacks[0]
    open_cards(page, live_app)
    held = []
    pattern = '**/audio-vibes/runs/' + first['id'] + '**'
    page.route(pattern, lambda route: held.append((route, route.fetch())))
    page.evaluate('''playback => {
      const cards = retentionCards;
      window.olderLoad = (async () => {
        await cards.accept({run_id: playback.id, playback, samples: [], prediction: null,
          taxonomy_version: cards.taxonomy, choices: JSON.parse(cards.root.dataset.choices)}, {placeholder: true});
        await cards.restore();
      })();
    }''', first)
    expect(visible_card(page)).to_be_visible()
    page.wait_for_function('retentionCards.loads.size === 1')
    other = page.context.new_page()
    try:
        open_cards(other, live_app)
        expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        visible_card(other).get_by_role('button', name='Dismiss vibe check for this song').click()
        add_retired_history(other, playbacks[1:])
        page.wait_for_function('id => !retentionCards.records().some(row => row.id === id)', arg=first['id'])
        assert_released(page, first['id'])
        assert len(held) == 1
        route, response = held.pop()
        route.fulfill(response=response)
        page.evaluate('() => olderLoad')
        page.unroute(pattern)
        assert_released(page, first['id'])
        assert not page.evaluate('id => retentionCards.records().some(row => row.id === id)', first['id'])
        # A repeated old full VIEW/sample response also has no admission power.
        page.evaluate('''async id => retentionCards.accept(
          await retentionCards.request('/audio-vibes/runs/' + id + '?later=1'))''', first['id'])
        assert_released(page, first['id'])
        expect(page.locator('[data-audio-vibe-banner]')).to_be_hidden()
    finally:
        for route, response in held:
            route.fulfill(response=response)
        other.close()


@pytest.mark.parametrize('change', ['add', 'remove'])
@pytest.mark.parametrize('recovery_status', [200, 503])
def test_memory_only_intent_recovers_latest_answer_after_tombstone_expires(
        vibes_enabled, live_app, page, store, change, recovery_status):
    values = seed_samples(store, count=2, video_id='old-unsaved-edit', title='Labels awaiting storage recovery')
    first = values[0]
    run_id = first['run_id']
    original_ids = [row['id'] for row in values]
    store.audio_vibes.save_feedback(run_id, original_ids, ['relax'], 'audio-session-targets-1', None, 10)
    history = completed(store, 80)
    open_cards(page, live_app)
    admit(page, {'id': run_id, **{key: first[key] for key in ('video_id', 'title', 'artist')}})
    other = page.context.new_page()
    try:
        open_cards(other, live_app)
        expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        page.evaluate('''key => {
          const original = Storage.prototype.setItem;
          window.failRetainedWrites = true;
          Storage.prototype.setItem = function(name, value) {
            if (name === key && failRetainedWrites) throw new DOMException('Quota exhausted', 'QuotaExceededError');
            return original.call(this, name, value);
          };
        }''', CARDS)
        key = 'energize' if change == 'add' else 'relax'
        visible_card(page).get_by_role('button', name=DESCRIPTIONS[key], exact=True).click()
        expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text(
            'Not saved yet · keep this page open.')
        visible_card(other).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True).click()
        expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        visible_card(other).get_by_role('button', name='Dismiss vibe check for this song').click()
        page.wait_for_function('id => retentionCards.records().find(row => row.id === id)?.tombstone', arg=run_id)
        add_retired_history(other, history)
        page.wait_for_function('id => !retentionCards.records().some(row => row.id === id)', arg=run_id)
        expect(visible_card(page)).to_be_visible()
        expect(visible_card(page).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_have_attribute(
            'aria-pressed', 'true')
        # Add later evidence without rewriting the click-time original IDs.
        later = {**sample(run_id=run_id, point='two_thirds'),
                 **{key: first[key] for key in ('video_id', 'title', 'artist')}}
        store.audio_vibes.save_sample(later, 30)
        page.evaluate('window.failRetainedWrites = false')
        if recovery_status != 200:
            pattern = '**/audio-vibes/runs/' + run_id + '**'
            page.route(pattern, lambda route: route.fulfill(status=recovery_status, json={}))
            visible_card(page).get_by_role('button', name='Retry saving', exact=True).click()
            expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text(
                'Not saved yet · keep this page open.')
            assert set(store.audio_vibes.feedback(run_id)['vibes']) == {'relax', 'cruise'}
            assert page.evaluate('retentionCards.edits.size') == 1
            expect(visible_card(page).get_by_role('button', name='Dismiss vibe check for this song')).to_be_disabled()
            if change == 'add':
                page.set_viewport_size({'width': 1280, 'height': 900})
                page.screenshot(path='/tmp/tuneconsole-886-recovery-offline.png')
            page.unroute(pattern)
        visible_card(page).get_by_role('button', name='Retry saving', exact=True).click()
        expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        answer = store.audio_vibes.feedback(run_id)
        expected = {'relax', 'cruise', 'energize'} if change == 'add' else {'cruise'}
        assert set(answer['vibes']) == expected
        assert answer['sample_ids'] == original_ids
        assert not page.evaluate('retentionCards.edits.size || retentionCards.storageFailed.size || retentionCards.recoveryFailed.size')
        visible_card(page).get_by_role('button', name='Dismiss vibe check for this song').click()
        assert_released(page, run_id)
    finally:
        other.close()


@pytest.mark.parametrize('status', [200, 503])
def test_dismissed_inflight_feedback_keeps_evidence_until_acknowledged(
        vibes_enabled, live_app, page, store, status):
    values = seed_samples(store, count=2)
    first = values[0]
    run_id = first['run_id']
    original_ids = [row['id'] for row in values]
    open_cards(page, live_app)
    held = []
    page.route('**/audio-vibes/feedback', lambda route: held.append(route))
    admit(page, {'id': run_id, **{key: first[key] for key in ('video_id', 'title', 'artist')}})
    visible_card(page).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    page.wait_for_function('retentionCards.saving')
    page.wait_for_timeout(100)
    visible_card(page).get_by_role('button', name='Dismiss vibe check for this song').click()
    assert_released(page, run_id)
    record = page.evaluate('id => retentionCards.records().find(row => row.id === id)', run_id)
    assert record['dirty'] and record['sample_ids'] == original_ids
    assert len(held) == 1
    if status == 200:
        held[0].continue_()
    else:
        held[0].fulfill(status=status, json={'detail': 'Temporary delivery failure'})
    page.wait_for_function('!retentionCards.saving')
    assert_released(page, run_id)
    record = page.evaluate('id => retentionCards.records().find(row => row.id === id)', run_id)
    if status == 200:
        assert record == {'id': run_id, 'dismissed': True, 'draft': ['relax'], 'tombstone': True}
        assert store.audio_vibes.feedback(run_id)['sample_ids'] == original_ids
    else:
        assert record['dirty'] and record['sample_ids'] == original_ids
        assert store.audio_vibes.feedback(run_id) is None


def test_previous_occurrence_answer_does_not_retire_an_unanswered_replay(
        vibes_enabled, live_app, page, store):
    first = completed(store, 1)[0]
    run_id = str(uuid4())
    store.audio_vibes.save_playback(run_id, first['video_id'], first['title'], first['artist'], 100)
    history = completed(store, 80)
    open_cards(page, live_app)
    admit(page, {**first, 'id': run_id})
    expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text('Previous answer')
    visible_card(page).get_by_role('button', name='Dismiss vibe check for this song').click()
    assert_released(page, run_id)
    add_retired_history(page, history)
    open_cards(page, live_app)
    record = page.evaluate('id => retentionCards.records().find(row => row.id === id)', run_id)
    assert record['previous_answer'] and record['draft'] == ['cruise']
    assert record['dismissed'] and not record.get('tombstone')
    assert record['playback']['video_id'] == first['video_id']
    assert store.audio_vibes.feedback(run_id) is None
