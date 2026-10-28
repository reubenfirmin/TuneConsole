"""Prepared mixes stay usable while preference changes and bounded refills catch up."""
from collections import Counter
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from tests.rec.test_home_prepared import prepared, seed, service
from tests.test_group_home import library as group_library
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.rec import choice_evidence, home_cards, home_prepared, home_vibe, mix_tuning, transient
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('change', ['vibe', 'genre', 'both', 'learning'])
def test_preferences_keep_prepared_mixes_and_explain_pending_update(store, prepared, monkeypatch, change):
    if change in ('genre', 'both'):
        with mix_tuning.adjusting(store, 1001):
            store.rec_proposals.put_proposals('home_genre_request', {
                'epoch': mix_tuning.card_epoch(store), 'include_genres': ['techno']}, 1001)
    if change in ('vibe', 'both'):
        with mix_tuning.adjusting_vibe(store, 1001):
            store.settings.set_setting(home_vibe.SETTING, 'energize')
    if change == 'learning':
        store.rec_proposals.put_proposals('choice_interests', {'interest_version': 'new-listening'}, 1001)

    def forbidden(*args, **kwargs):
        pytest.fail('An ordinary Home request must use prepared mixes without assembly')

    monkeypatch.setattr(home_cards.HomeCards, 'cards', forbidden)
    scheduled = []
    payload = home_prepared.load(store, 1002, lambda: scheduled.append(True))
    result = home_prepared.cards(store, payload, 1002, service(store), lambda: scheduled.append(True), record=False)
    assert len(result.protos) == 4 and scheduled
    assert result.preparing
    if change in ('vibe', 'both'):
        combination = 'energize + techno' if change == 'both' else 'energize'
        assert result.preference_notice == f"Heads up - we're unable to create {combination} mix tapes right now."
        assert all(not p['recipe'].get('vibe') for p in result.protos)
    elif change == 'genre':
        assert result.preference_notice == "Heads up - we're unable to create techno mix tapes right now."
    else:
        assert result.preference_notice is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


def test_tuned_set_survives_learning_refresh_but_expiry_prefers_baseline(store, prepared):
    with mix_tuning.adjusting_vibe(store, 1001):
        store.settings.set_setting(home_vibe.SETTING, 'energize')
    tuned_result = service(store).cards(home_cards.CardsRequest(1001, record=False))
    home_prepared.remember(store, prepared, tuned_result, 1001)
    tuned = home_prepared.load(store, 1002)
    store.rec_proposals.put_proposals('choice_interests', {'interest_version': 'later-listening'}, 1003)
    stale = home_prepared.load(store, 1004)
    assert stale['tuning'] == tuned['tuning']
    result = home_prepared.cards(store, stale, 1004, service(store), record=False)
    assert result.protos and all(p['recipe']['vibe'] == 'energize' for p in result.protos)
    assert result.preparing and result.preference_notice is None
    assert home_prepared.load(store, 1001 + mix_tuning.TTL_S)['tuning'] == prepared['tuning']


def test_updated_set_replaces_fallback_in_same_window_and_clears_notice(store, prepared):
    with mix_tuning.adjusting_vibe(store, 1001):
        store.settings.set_setting(home_vibe.SETTING, 'energize')
    fallback = home_prepared.cards(store, prepared, 1002, service(store))
    assert fallback.protos and fallback.preference_notice
    app = create_app(store, lambda: {}, now_fn=lambda: 1002, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
        assert 'role="status"' in response.text and 'unable to create energize mix tapes' in response.text
        assert 'hx-get="/home/cards?prepare_attempt=1"' in response.text
        refreshed = deepcopy(prepared)
        refreshed['tuning'] = home_prepared.tuning_key(store, 1002)
        for menu in refreshed['menus']:
            for proto in menu['protos']:
                proto['name'] = 'Updated for Energize'
                proto['recipe']['vibe'] = 'energize'
        store.rec_proposals.put_proposals('home_tuned', refreshed, 1002)
        response = client.get('/home/cards?prepare_attempt=1')
        assert 'Updated for Energize' in response.text
        assert 'unable to create energize mix tapes' not in response.text
        assert 'id="home-mix-status"' in response.text and 'hidden' in response.text
        assert 'hx-get="/home/cards?prepare_attempt=' not in response.text


def test_stale_mixes_still_apply_new_dislikes(store, prepared):
    first = home_prepared.cards(store, prepared, 1000, service(store), record=False)
    key = first.protos[0]['tracks'][0]['key']
    with mix_tuning.adjusting_vibe(store, 1001):
        store.settings.set_setting(home_vibe.SETTING, 'relax')
    store.rec_model.record_dislike(key, until=None, now=1001)
    result = home_prepared.cards(store, home_prepared.load(store, 1002), 1002, service(store), record=False)
    assert result.protos and result.preference_notice
    assert key not in {t['key'] for p in result.protos for t in p['tracks']}


def test_vibe_fresh_reuses_prepared_recipe_inputs(store, monkeypatch):
    from yt_playlist.rec import recipes

    seed(store)
    with mix_tuning.adjusting_vibe(store, 1001):
        store.settings.set_setting(home_vibe.SETTING, 'energize')
    parameters = {'include_genres': [], 'avoid_genres': []}
    expected = service(store).built_fresh(parameters, 1002)
    assert expected and len(expected['tracks']) >= 4
    preparation = {
        'leans': transient.facet_leans(store, 1002),
        'dists': (recipes.taste_breadth(store)['families'], dict(recipes.era_distribution(store))),
    }

    def forbidden(*args, **kwargs):
        pytest.fail('Fresh must reuse captured recipe inputs during a bounded refill')

    monkeypatch.setattr(recipes, 'taste_breadth', forbidden)
    monkeypatch.setattr(recipes, 'era_distribution', forbidden)
    monkeypatch.setattr(transient, 'facet_leans', forbidden)
    home = service(store)
    home.selection_data = {'preparation': preparation}
    assert home.built_fresh(parameters, 1002) == expected


@pytest.mark.parametrize('blocked_tuned,blocked_baseline,source,count', [
    (0, 0, 'tuned', 4), (1, 0, 'baseline', 4), (4, 0, 'baseline', 4),
    (1, 2, 'tuned', 3), (4, 4, 'tuned', 0),
])
def test_eligibility_chooses_usable_cache_before_recording(
        store, prepared, monkeypatch, blocked_tuned, blocked_baseline, source, count):
    bundles = store.rec_proposals.get_proposals('mode_bundles')
    caches = {}
    for name, offset in (('baseline', 0), ('tuned', 12)):
        payload = deepcopy(prepared)
        menu = payload['menus'][0]
        payload['menus'] = [menu]
        menu['epoch'] = 0
        payload['generation'] = name
        payload['choice_context'] = {'capture_status': name}
        for proto in menu['protos']:
            proto['tracks'] = deepcopy(bundles['all'][proto['lane']][offset:offset + 12])
            assert len(proto['tracks']) == 12
            if name == 'tuned':
                proto['recipe']['vibe'] = 'energize'
            proto['experiment_id'] = home_cards.experiment_id(proto)
            proto['recipe']['experiment_id'] = proto['experiment_id']
        by_lane = {p['lane']: p for p in menu['protos']}
        for slot in menu['slots']:
            slot['proto'] = by_lane[slot['proto']['lane']]
        caches[name] = payload
    keys = lambda payload: {t['key'] for p in payload['menus'][0]['protos'] for t in p['tracks']}
    assert not keys(caches['baseline']) & keys(caches['tuned'])
    with mix_tuning.adjusting_vibe(store, 1001):
        store.settings.set_setting(home_vibe.SETTING, 'energize')
    caches['tuned']['tuning'] = home_prepared.tuning_key(store, 1002)
    for name, surface in (('baseline', home_prepared.SURFACE), ('tuned', 'home_tuned')):
        assert home_prepared._valid(caches[name])
        store.rec_proposals.put_proposals(surface, caches[name], 1001)
    excluded = set()
    for name, blocked in (('tuned', blocked_tuned), ('baseline', blocked_baseline)):
        for proto in caches[name]['menus'][0]['protos'][:blocked]:
            excluded.update(t['key'] for t in proto['tracks'])
    for key in excluded:
        store.rec_model.record_dislike(key, until=None, now=1002)
    offered, impressions, scheduled = [], [], []
    home = service(store)
    record_offer, record_modes = home.record_offer, home.record_modes

    def capture_offer(protos, now, **kwargs):
        offered.append(deepcopy(protos))
        return record_offer(protos, now, **kwargs)

    def capture_modes(protos, epoch, now):
        impressions.append(deepcopy(protos))
        return record_modes(protos, epoch, now)

    monkeypatch.setattr(home, 'record_offer', capture_offer)
    monkeypatch.setattr(home, 'record_modes', capture_modes)
    expected = keys(caches[source]) - excluded
    for now in (1003, 1004):
        payload = home_prepared.load(store, now, lambda: scheduled.append(True))
        assert payload == caches['tuned']
        result = home_prepared.cards(store, payload, now, home, lambda: scheduled.append(True))
        assert len(result.protos) == count
        assert {t['key'] for p in result.protos for t in p['tracks']} == expected
        assert result.choice_context == caches[source]['choice_context']
        assert bool(result.preference_notice) == (source == 'baseline')
        assert result.preparing == (source == 'baseline' or count < 4)
        held = store.rec_proposals.get_proposals('home_choice')
        assert held['prepared_tuning'] == caches[source]['tuning']
        assert held['assignment']['generation'] == source
    assert len(offered) == len(impressions) == (2 if count else 0)
    assert all({t['key'] for p in protos for t in p['tracks']} == expected
               for protos in offered + impressions)
    assert bool(scheduled) == bool(blocked_tuned)
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == caches['baseline']


@pytest.mark.parametrize('selection', ['include', 'avoid'])
@pytest.mark.parametrize('exhausted', [False, True])
def test_partial_genre_menu_keeps_usable_offers_before_falling_back(
        store, prepared, selection, exhausted):
    request = ({'include_genres': ['jazz']} if selection == 'include' else
               {'avoid_genres': ['ambient', 'classical', 'rock-indie', 'techno', 'trance']})
    with mix_tuning.adjusting(store, 1001):
        store.rec_proposals.put_proposals('home_genre_request', {
            'epoch': mix_tuning.card_epoch(store), **request}, 1001)
    tailored = home_prepared.prepare(store, 1001)
    assert tailored and all(0 < len(menu['protos']) < 4 for menu in tailored['menus'])
    keys = {t['key'] for menu in tailored['menus'] for p in menu['protos'] for t in p['tracks']}
    if exhausted:
        for key in keys:
            store.rec_model.record_dislike(key, until=None, now=1002)

    scheduled = []
    for now in (1003, 1004):
        payload = home_prepared.load(store, now)
        assert payload == tailored
        shown = home_prepared.cards(store, payload, now, service(store), lambda: scheduled.append(True))
        assert shown.protos and shown.preparing
        held = store.rec_proposals.get_proposals('home_choice')
        if exhausted:
            assert shown.preference_notice
            assert held['prepared_tuning'] == prepared['tuning']
            assert not keys & {t['key'] for p in shown.protos for t in p['tracks']}
        else:
            assert shown.preference_notice is None
            assert held['prepared_tuning'] == tailored['tuning']
            contents = lambda protos: [(p['experiment_id'], [t['key'] for t in p['tracks']]) for p in protos]
            assert any(contents(shown.protos) == contents(menu['protos']) for menu in tailored['menus'])
            assert all(t['genre'] == 'Jazz' for p in shown.protos for t in p['tracks'])
            assert all(p['recipe']['genre_request'].get(selection) == request[f'{selection}_genres']
                       for p in shown.protos)
            if now == 1003:
                initial = contents(shown.protos)
            else:
                assert contents(shown.protos) == initial
    assert scheduled
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


def test_retry_reuses_completed_card_work_without_publishing_partial_set(store, monkeypatch):
    seed(store)
    track = store.conn.execute('SELECT identity_key FROM tracks LIMIT 1').fetchone()[0]
    store.rec_model.record_dislike(track, until=None, now=900)
    complete = home_cards._complete_card
    completed = Counter()
    failed = []

    def limited(reader, card, *args):
        if card['lane'] == 'explore' and not failed:
            failed.append(True)
            raise WorkBudgetExceeded('One card completed before this attempt ended')
        completed[card['lane']] += 1
        return complete(reader, card, *args)

    monkeypatch.setattr(home_cards, '_complete_card', limited)
    assert home_prepared.prepare(store, 1000) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) is None
    payload = home_prepared.prepare(store, 1001)
    assert payload and payload['menus'][0]['protos']
    assert completed['wheelhouse'] == len(payload['menus'])
    assert not store.modes.impression_counts()


def test_adaptive_pool_decode_survives_deferred_setup_and_invalidates_on_replacement(store, monkeypatch):
    from yt_playlist.repos.rec_proposals import RecProposalsRepo
    group_library(store)
    real_read = RecProposalsRepo.group_pools_json
    reads = []

    def counted(repo):
        value = real_read(repo)
        reads.append(value)
        return value

    def defer(*args, **kwargs):
        raise WorkBudgetExceeded('Completed pool capture must survive later setup deferral')

    monkeypatch.setattr(RecProposalsRepo, 'group_pools_json', counted)
    with monkeypatch.context() as patch:
        patch.setattr(home_prepared, 'tuning_key', defer)
        assert home_prepared.prepare(store, 1200) is None
        assert home_prepared.prepare(store, 1201) is None
    assert len(reads) == 1
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) is None
    payload = home_prepared.prepare(store, 1202)
    assert payload and payload['menus'][0]['protos']
    assert len(reads) == 1
    bundles = store.rec_proposals.get_proposals('mode_bundles')
    bundles['_meta']['pool_replacement'] = True
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1203)
    with monkeypatch.context() as patch:
        patch.setattr(home_prepared, 'tuning_key', defer)
        assert home_prepared.prepare(store, 1204) is None
    assert len(reads) == 2 and reads[0] != reads[1]
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == payload


def test_context_and_mix_preparation_share_signals_across_retries_and_expire(store, monkeypatch):
    seed(store)
    track = store.conn.execute('SELECT identity_key FROM tracks LIMIT 1').fetchone()[0]
    store.rec_model.record_dislike(track, until=None, now=900)
    calculate, capture = transient.facet_leans, transient.prepared_facet_leans
    calls, phase, capturing = [], ['context'], [False]

    def counted(reader, now):
        calls.append((phase[0], capturing[0], now))
        return calculate(reader, now)

    def shared(reader, now):
        capturing[0] = True
        try:
            return capture(reader, now)
        finally:
            capturing[0] = False

    def interrupted(*args):
        raise WorkBudgetExceeded('Retry after shared inputs, before the first complete card')

    monkeypatch.setattr(transient, 'facet_leans', counted)
    monkeypatch.setattr(transient, 'prepared_facet_leans', shared)
    monkeypatch.setattr(home_cards, '_complete_card', interrupted)
    assert choice_evidence.prepare_context(store, 1000)
    phase[0] = 'mixes'
    assert home_prepared.prepare(store, 1000) is None
    # Observe callers independently: discovery may calculate current signals,
    # while the retained prepared capture is shared across context/mix retries.
    assert [(p, at) for p, prepared, at in calls if prepared] == [('context', 1000)]
    assert any(p == 'mixes' and not prepared for p, prepared, _ in calls)
    phase[0] = 'context-retry'
    assert choice_evidence.prepare_context(store, 1030)
    phase[0] = 'mixes-retry'
    assert home_prepared.prepare(store, 1030) is None
    assert [(p, at) for p, prepared, at in calls if prepared] == [('context', 1000)]
    phase[0] = 'retained'
    with store.read_snapshot() as snapshot:
        old = transient.prepared_facet_leans(snapshot.store, 1040)
    assert not any(p == 'retained' for p, _, _ in calls)

    # Exercise actual LRU eviction rather than assuming the signal entry must
    # survive unrelated completed-card/menu captures forever.
    with store.read_snapshot() as snapshot:
        for index in range(store._read_memo._limit + 1):
            snapshot.store.memoized_read(('eviction-control', index), lambda: {})
    phase[0] = 'evicted'
    with store.read_snapshot() as snapshot:
        refreshed = transient.prepared_facet_leans(snapshot.store, 1041)
    assert ('evicted', True, 1041) in calls and refreshed != old
    phase[0] = 'retained-after-eviction'
    with store.read_snapshot() as snapshot:
        assert transient.prepared_facet_leans(snapshot.store, 1042) == refreshed
    assert not any(p == 'retained-after-eviction' for p, _, _ in calls)
    phase[0] = 'bucket-expiry'
    with store.read_snapshot() as snapshot:
        aged = transient.prepared_facet_leans(snapshot.store, 1200)
    assert aged != refreshed and ('bucket-expiry', True, 1200) in calls
    another = store.conn.execute('SELECT identity_key FROM tracks WHERE identity_key<>? LIMIT 1',
                                 (track,)).fetchone()[0]
    assert store.rec_model.record_dislike(another, until=None, now=1201)
    phase[0] = 'same-bucket-feedback'
    with store.read_snapshot() as snapshot:
        corrected = transient.prepared_facet_leans(snapshot.store, 1202)
    assert corrected != aged and ('same-bucket-feedback', True, 1202) in calls


@pytest.mark.parametrize('new_feedback', [False, True])
def test_graph_retry_reuses_completed_selection_until_its_inputs_change(store, monkeypatch, new_feedback):
    group_library(store)
    assemble, complete = home_cards._group_cards, home_cards._complete_card
    calls, selected, failed = Counter(), [], []

    def counted(reader, now, epoch, *args):
        calls[epoch] += 1
        return assemble(reader, now, epoch, *args)

    def limited(reader, card, *args):
        if card['lane'] == 'explore' and not failed:
            failed.append(True)
            raise WorkBudgetExceeded('Selection and the first card completed')
        selected.extend(t['key'] for t in card['tracks'])
        return complete(reader, card, *args)

    monkeypatch.setattr(home_cards, '_group_cards', counted)
    monkeypatch.setattr(home_cards, '_complete_card', limited)
    assert home_prepared.prepare(store, 1200) is None
    assert selected
    if new_feedback:
        store.rec_model.record_dislike(selected[0], until=None, now=1201)
    payload = home_prepared.prepare(store, 1230)
    assert payload and home_prepared._valid(payload)
    assert calls[0] == (2 if new_feedback else 1)
    if new_feedback:
        assert selected[0] not in {t['key'] for m in payload['menus'] for p in m['protos'] for t in p['tracks']}
