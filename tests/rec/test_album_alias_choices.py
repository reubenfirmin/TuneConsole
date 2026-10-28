"""Verified release identity groups choice dates without rewriting evidence."""
from copy import deepcopy

import numpy as np
import pytest

from tests import model_support
from tests.repos.test_album_aliases import OLD, ALIAS, release
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.rec import choice_evidence, choice_learning, rec_params, scoring

DAY = 86400
START = 1_700_000_000
NOW = START + 4 * DAY


def raw(store):
    ledger = store.rec_ledger.evidence()
    return {kind: deepcopy(ledger.records(kind)) for kind in ('opportunity', 'response')}, {
        str(path.relative_to(ledger.directory)): path.read_bytes()
        for kind in ('opportunity', 'response')
        for path in (ledger.directory / kind).rglob('*.json')}


def setup_choices(store, first, second, before, scenario, distinct=False):
    payload = release()
    remember_album(store, first, payload)
    tracks = []
    vectors = []
    for index, track in enumerate(payload['tracks']):
        key = f'track {index}|all them witches'
        entry = store.tracks.upsert_track(track['videoId'], track['title'], 'All Them Witches',
                                         payload['title'], 181, album_browse_id=first)
        store.tracks.set_track_enrichment(entry, 'Rock', '2015')
        tracks.append({'key': key, 'video_id': track['videoId']})
        vectors.append((key, np.array([1., 0.], dtype=np.float32).tobytes()))
    store.collection.add_saved_album({'browse': 'anchor', 'title': 'Anchor', 'artist': 'Anchor'})
    entry = store.tracks.upsert_track('anchor-video', 'Anchor', 'Anchor', 'Anchor', 181,
                                     album_browse_id='anchor')
    store.tracks.set_track_enrichment(entry, 'Folk', '1990')
    vectors.append(('anchor|anchor', np.array([0., 1.], dtype=np.float32).tobytes()))
    store.rec_model.replace_rec_vectors(vectors)
    model_support.build_content_model(store)
    other = deepcopy(payload)
    if distinct:
        other['year'] = '2016'
    if not before:
        remember_album(store, second, other)
    events = []
    for day in range(3):
        ids = ([first] if scenario == 'preferred' else [second] if scenario == 'alias' else
               [(first, second, first)[day]] if scenario == 'split' else [first, second])
        for copy, browse in enumerate(ids):
            oid = f'choice:{day}:{copy}'
            at = START + day * DAY
            choice_evidence.issue(store, 'albums', [{'id': browse, 'tracks': tracks}], at,
                context={}, opportunity_id=oid, episode_id=oid)
            choice_evidence.respond(store, oid, 'choice', at + 1, option_id=browse, action_id=oid)
            events.append((day, oid))
    if before:
        choice_evidence.refresh_report(store, NOW)
        remember_album(store, second, other)
    assert store.collection.saved_album_ids().isdisjoint({first, second})
    assert not store.rec_query.play_counts()
    return events


def measure(store, now):
    frozen = raw(store)
    choice_evidence.refresh_report(store, now)
    projection = store.rec_proposals.get_proposals('choice_interests')
    assert not projection.get('album_track_days')
    contexts = [row for row in choice_learning.contexts(store, now) if row['format'] == 'albums']
    tastes = (scoring.playlist_taste(store, now=now), scoring.content_taste(store, now=now))
    assert raw(store) == frozen
    return contexts, tastes


def assert_release(store, now, days, latest):
    contexts, tastes = measure(store, now)
    context, = contexts
    assert context['days'] == days
    standing = .5 if days >= 3 else 0.
    assert context['layer_weights']['saved'] == pytest.approx(standing)
    assert context['layer_weights']['now'] == context['layer_weights']['session'] == 0
    recent = rec_params.get_param(store, 'choice_recent_w') * 2 ** (
        -(now - latest) / (DAY * rec_params.get_param(store, 'choice_halflife_d')))
    assert context['layer_weights']['recent'] == pytest.approx(recent)
    for taste in tastes:
        index, = [i for i, source in enumerate(taste.sources) if source['kind'] == 'albums']
        assert taste.sources[index]['days'] == days
        expected = standing + recent
        assert taste.weights[index] == pytest.approx(expected / (1 + expected))


@pytest.mark.parametrize('first', [OLD, ALIAS])
@pytest.mark.parametrize('before', [True, False])
@pytest.mark.parametrize('scenario', ['preferred', 'alias', 'split', 'overlap'])
def test_unsaved_choice_dates_combine_once_in_both_vector_spaces(store, first, before, scenario):
    second = ALIAS if first == OLD else OLD
    setup_choices(store, first, second, before, scenario)
    original = raw(store)
    assert store.collection.album_id(first) == store.collection.album_id(second)
    assert_release(store, NOW + 1, 3, START + 2 * DAY + 1)
    assert raw(store) == original


@pytest.mark.parametrize('first', [OLD, ALIAS])
@pytest.mark.parametrize('before', [True, False])
def test_withdrawal_keeps_a_shared_date_until_its_last_choice_is_withdrawn(store, first, before):
    second = ALIAS if first == OLD else OLD
    events = setup_choices(store, first, second, before, 'overlap')
    original, files = raw(store)
    assert_release(store, NOW + 1, 3, START + 2 * DAY + 1)
    for index, (_, oid) in enumerate(event for event in events if event[0] == 2):
        choice_evidence.respond(store, oid, 'withdraw', NOW + 2 + index,
            action_id=f'withdraw:{index}', details={'action_id': oid})
        assert_release(store, NOW + 2 + index, 3 if index == 0 else 2,
                       START + (2 if index == 0 else 1) * DAY + 1)
    current, current_files = raw(store)
    assert current['opportunity'] == original['opportunity']
    assert all(row in current['response'] for row in original['response'])
    assert all(current_files.get(path) == value for path, value in files.items())


@pytest.mark.parametrize('first', [OLD, ALIAS])
@pytest.mark.parametrize('scenario', ['split', 'overlap'])
def test_distinct_editions_keep_separate_choice_dates(store, first, scenario):
    second = ALIAS if first == OLD else OLD
    setup_choices(store, first, second, False, scenario, distinct=True)
    assert store.collection.album_id(first) != store.collection.album_id(second)
    contexts, tastes = measure(store, NOW + 1)
    assert {row['option_id']: row['days'] for row in contexts} == (
        {first: 2, second: 1} if scenario == 'split' else {first: 3, second: 3})
    assert all(sum(source['kind'] == 'albums' for source in taste.sources) == 2 for taste in tastes)
