"""Home display reuse preserves evidence and follows physical publication health."""
from pathlib import Path
from copy import deepcopy
import json
import logging

import pytest

from tests.home_fixture_support import publish_menu
from yt_playlist.core import fao, home_outputs, json_cache
from yt_playlist.rec import home_cache, home_cards, home_prepared


def cards(store, payload, *, record=False):
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    return home_prepared.cards(store, payload, 1000, service, record=record)


def test_home_display_keeps_full_mix_evidence_and_detaches_reused_results(file_backed_store):
    store = file_backed_store
    payload = publish_menu(store)
    evidence = {'features': {'tempo': 124, 'confidence': .75}, 'sources': ['provider', 'audio']}
    recipe = {'lane': 'comfort', 'vibe': 'energetic', 'genre_request': {'include': ['house']}}
    payload['menus'][0]['protos'][0]['tracks'][0]['musical'] = evidence
    payload['menus'][0]['protos'][0]['recipe'] = recipe
    payload['choice_context'] = {'frozen_evidence': {'observations': [1, 2, 3]}}
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1000)

    first = home_prepared.load(store, 1000, display_only=True)
    assert 'musical' not in first['menus'][0]['protos'][0]['tracks'][0]
    assert first['choice_context'] == payload['choice_context']
    selected = cards(store, first, record=True)
    assert selected.protos[0]['tracks'][0]['musical'] == evidence
    assert selected.protos[0]['recipe'] == recipe
    assert store.rec_proposals.get_proposals('home_choice')['menu'] == payload['menus'][0]
    selected.protos[0]['tracks'][0]['musical']['sources'].append('changed')
    selected.protos[0]['recipe']['genre_request']['include'].append('changed')
    first['choice_context']['frozen_evidence']['observations'].append(4)
    following = home_prepared.load(store, 1000, display_only=True)
    restored = cards(store, following)
    assert restored.protos[0]['tracks'][0]['musical'] == evidence
    assert restored.protos[0]['recipe'] == recipe
    assert following['choice_context'] == payload['choice_context']


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_cached_display_falls_back_after_file_damage_and_follows_republication(file_backed_store, damage):
    store = file_backed_store
    baseline = publish_menu(store, prefix='baseline')
    tailored = publish_menu(store, prefix='tailored', surface='home_tuned')
    assert cards(store, home_prepared.load(store, 1000, display_only=True)).protos == cards(store, tailored).protos
    filename = store.conn.execute('SELECT f.filename FROM home_display_files f '
        'JOIN home_display_heads h ON h.publication_id=f.publication_id '
        "WHERE h.surface='home_tuned' LIMIT 1").fetchone()[0]
    if damage == 'missing':
        Path(filename).unlink()
    else:
        fao.write(filename, b'Invalid replacement document')
    fallback = home_prepared.load(store, 1000, display_only=True)
    assert [p['experiment_id'] for p in fallback['menus'][0]['protos']] == [
        p['experiment_id'] for p in baseline['menus'][0]['protos']]
    assert home_prepared._load_surface(store, 'home_tuned', display_only=True) is None
    replacement = publish_menu(store, prefix='replacement', surface='home_tuned')
    assert cards(store, home_prepared.load(store, 1000, display_only=True)).protos == cards(store, replacement).protos


@pytest.mark.parametrize('compressed', [False, True])
def test_display_restores_only_selected_menu_and_preserves_the_same_assignment(file_backed_store, monkeypatch,
                                                                            compressed):
    store = file_backed_store
    payload = publish_menu(store)
    menu = payload['menus'][0]
    payload['menus'] = [deepcopy(menu) for _ in range(12)]
    for index, value in enumerate(payload['menus']):
        value['epoch'] = index
        for proto in value['protos']:
            for track in proto['tracks']:
                track['musical'] = {'selected_menu': index, 'observations': list(range(300))}
                track['serving_context'] = {'checkpoint': index, 'observations': list(range(300))}
    # Use both installed plain JSON and the shared-profile representation.
    encoded = json_cache.encode(payload) if compressed else json.dumps(payload)
    publication = home_outputs.prepare(home_prepared.SURFACE, encoded, 1000)
    with store.conn.atomic():
        store.rec_proposals.publish_prepared_home(publication, 1000)
    expected = cards(store, payload)
    restored_menus = []
    original = json_cache._restore_profiles

    def restore(value, profiles, *args, **kwargs):
        if isinstance(value, dict) and 'musical' in value:
            restored_menus.append(value['musical'])
        return original(value, profiles, *args, **kwargs)

    monkeypatch.setattr(json_cache, '_restore_profiles', restore)
    projected = home_prepared.load(store, 1000, display_only=True)
    # Unselected checkpoints must not dominate the detached selection copy.
    # The selected result below must still carry every original field.
    assert len(json.dumps(projected)) < len(json.dumps(payload)) / 4
    actual = cards(store, projected)
    assert actual == expected
    if compressed:
        assert restored_menus
        # Only the selected menu's evidence enters restoration. This fixture's
        # slot copies have no profiles; none of the other eleven menus is read.
        assert len(restored_menus) == sum(len(proto['tracks']) for proto in expected.protos)
    actual.protos[0]['tracks'][0]['musical']['observations'].append('mutated')
    assert cards(store, home_prepared.load(store, 1000, display_only=True)) == expected


def test_display_preserves_a_whole_held_menu_with_its_frozen_evidence(file_backed_store):
    from tests.derived_rating_support import seed_dislike

    store = file_backed_store
    first = publish_menu(store, prefix='first')
    second = publish_menu(store, prefix='second')
    second['menus'][0]['epoch'] = first['menus'][0]['epoch'] + 3
    evidence = {'origin': 'held-worker-menu', 'observations': [2, 4, 6]}
    first['menus'][0]['protos'][0]['tracks'][0]['musical'] = evidence
    payload = {**first, 'menus': [first['menus'][0], second['menus'][0]]}
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1000)
    store.rec_proposals.put_proposals('home_choice', {
        'epoch': home_cards._epoch(store, 'cards'), 'tuning': payload['tuning'],
        'experiments': [proto['experiment_id'] for proto in first['menus'][0]['protos']],
        'menu': first['menus'][0], 'choice_context': first['choice_context'],
        'assignment': {'policy': 'test', 'probability': .5}}, 1000)
    seed_dislike(store, first['menus'][0]['protos'][0]['tracks'][0]['key'], until=None, now=999)
    expected = cards(store, payload)
    actual = cards(store, home_prepared.load(store, 1000, display_only=True), record=True)
    assert actual == expected
    assert actual.protos[0]['tracks'][0]['musical'] == evidence
    assert not actual.protos[0]['assignment'].get('censored')
    assert [p['tracks'] for p in actual.protos] == [p['tracks'] for p in first['menus'][0]['protos']]
    held = store.rec_proposals.get_proposals('home_choice')
    assert held['menu'] == first['menus'][0]


def test_one_home_composition_verifies_each_document_once_but_next_read_checks_files(file_backed_store, monkeypatch):
    from yt_playlist.core import home_outputs

    store = file_backed_store
    payload = publish_menu(store)
    original = home_outputs.files.read
    verified = []

    def read(*args, **kwargs):
        verified.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(home_outputs.files, 'read', read)
    with store.read_view(tracked=True) as reader:
        with home_cache.verified_reads(reader):
            full = home_cache.read(reader, home_prepared.SURFACE)
            assert full['menus'] == payload['menus']
            first = list(verified)
            assert first
            projected = home_cache.read(reader, home_prepared.SURFACE, fields=('menus',))
            projected['menus'][0]['protos'].clear()
            assert home_cache.read(reader, home_prepared.SURFACE)['menus'] == payload['menus']
            assert verified == first
        # Leaving the composition ends reuse even on this same pinned reader.
        home_cache.read(reader, home_prepared.SURFACE)
        assert verified == first + first
    Path(first[0]).unlink()
    with store.read_view(tracked=True) as reader, home_cache.verified_reads(reader):
        assert home_cache.read(reader, home_prepared.SURFACE) is None


def test_verified_home_reuse_retains_dependencies_for_memo_invalidation(file_backed_store):
    store = file_backed_store
    original = publish_menu(store, prefix='first')
    name = 'home-projection-after-physical-read'

    def projection(reader):
        return home_cache.read(reader, home_prepared.SURFACE, fields=('menus',))

    with store.read_view(tracked=True) as reader, home_cache.verified_reads(reader):
        # The physical read happens outside the nested memo's capture.
        home_cache.read(reader, home_prepared.SURFACE)
        assert reader.memoized_read(name, lambda: projection(reader))['menus'] == original['menus']
    replacement = publish_menu(store, prefix='replacement')
    with store.read_view(tracked=True) as reader, home_cache.verified_reads(reader):
        assert reader.memoized_read(name, lambda: projection(reader))['menus'] == replacement['menus']
