"""Verified release identity groups choice dates without rewriting evidence."""
from copy import deepcopy
from contextlib import ExitStack
from tests.enrichment_support import seed_enrichment, use_existing_album_tracks

import numpy as np
import pytest

from tests import model_support
from tests.prepared_fixture_support import prepared_template
from tests.store_fixture_support import bounded_consumer_schema, reuse_reporting_reader
from tests.repos.test_album_aliases import OLD, ALIAS, release
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.rec import choice_evidence, choice_learning, rec_params, scoring

DAY = 86400
START = 1_700_000_000
NOW = START + 4 * DAY


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Scoring and evidence tests do not construct the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('rock', 'Rock'), ('folk', 'Folk'))) as schema:
        yield schema


@pytest.fixture
def store(choice_catalogs, first, tmp_path, monkeypatch):
    directory = tmp_path / 'album-choice'
    directory.mkdir()
    with choice_catalogs(first).open(directory / 'library.db') as database:
        use_existing_album_tracks(database, monkeypatch)
        with reuse_reporting_reader(database, monkeypatch):
            yield database


def raw(store):
    ledger = store.rec_ledger.evidence()
    return {kind: deepcopy(ledger.records(kind)) for kind in ('opportunity', 'response')}, {
        str(path.relative_to(ledger.directory)): path.read_bytes()
        for kind in ('opportunity', 'response')
        for path in (ledger.directory / kind).rglob('*.json')}


def seed_choice_catalog(store, first):
    payload = release()
    remember_album(store, first, payload)
    tracks = []
    vectors = []
    for index, track in enumerate(payload['tracks']):
        key = f'track {index}|all them witches'
        entry = store.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?',
                                   (track['videoId'],)).fetchone()[0]
        with store.conn:
            store.conn.execute("INSERT OR IGNORE INTO library_memberships VALUES (?,'fixture',NULL)", (entry,))
        seed_enrichment(store, entry, 'Rock', '2015')
        tracks.append({'key': key, 'video_id': track['videoId']})
        vectors.append((key, np.array([1., 0.], dtype=np.float32).tobytes()))
    store.collection.add_saved_album({'browse': 'anchor', 'title': 'Anchor', 'artist': 'Anchor'})
    entry = store.tracks.upsert_track('anchor-video', 'Anchor', 'Anchor', 'Anchor', 181,
                                     album_browse_id='anchor')
    seed_enrichment(store, entry, 'Folk', '1990')
    vectors.append(('anchor|anchor', np.array([0., 1.], dtype=np.float32).tobytes()))
    store.rec_model.replace_rec_vectors(vectors)
    model_support.build_content_model(store)
    return payload, tracks


@pytest.fixture(scope='module')
def choice_catalogs(_pristine_current_schema, tmp_path_factory):
    with ExitStack() as resources:
        templates = {}

        def template(first):
            if first not in templates:
                def build(source):
                    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(source, patch):
                        use_existing_album_tracks(source, patch)
                        return seed_choice_catalog(source, first)
                templates[first] = resources.enter_context(prepared_template(
                    _pristine_current_schema, build,
                    database=tmp_path_factory.mktemp('album-choice-catalog') / 'source.db'))
            return templates[first]

        yield template


@pytest.fixture
def choice_catalog(store, choice_catalogs, first):
    # The Store already owns one complete private copy, including retained files.
    return deepcopy(choice_catalogs(first).result)


@pytest.mark.parametrize('first', [OLD, ALIAS])
def test_choice_catalog_copies_keep_models_payloads_and_aliases_private(
        store, file_backed_store, first, choice_catalogs, choice_catalog):
    from tests.prepared_fixture_support import roots

    template = choice_catalogs(first)
    second = ALIAS if first == OLD else OLD
    other_catalog = template.restore(file_backed_store)
    assert choice_catalog == other_catalog == template.result
    vectors = (template.source.rec_model.get_rec_vectors(),
               template.source.rec_model.get_rec_content_vectors())
    assert len(vectors[0]) == len(vectors[1]) == 10
    namespaces = [set(roots(target.conn).values()) for target in (template.source, store, file_backed_store)]
    assert all(left.isdisjoint(right) for index, left in enumerate(namespaces) for right in namespaces[index + 1:])
    for target in (store, file_backed_store):
        assert (target.rec_model.get_rec_vectors(), target.rec_model.get_rec_content_vectors()) == vectors
        assert target.collection.album_id(second) is None
        assert not target.rec_query.play_counts()
    payload, tracks = choice_catalog
    remember_album(store, second, payload)
    assert store.collection.album_id(first) == store.collection.album_id(second)
    payload['year'] = 'Private payload edit'
    tracks[0]['video_id'] = 'private-video'
    store.rec_model.replace_rec_vectors([('anchor|anchor', np.array([1., 1.], dtype=np.float32).tobytes())])
    assert other_catalog == template.result
    for other in (template.source, file_backed_store):
        assert other.collection.album_id(second) is None
        assert (other.rec_model.get_rec_vectors(), other.rec_model.get_rec_content_vectors()) == vectors


def setup_choices(store, first, second, before, scenario, *, catalog, distinct=False):
    payload, tracks = catalog
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
@pytest.mark.parametrize('scenario', ['preferred', 'alias', 'split'])
def test_unsaved_choice_dates_combine_once_in_both_vector_spaces(store, first, before, scenario, choice_catalog):
    second = ALIAS if first == OLD else OLD
    setup_choices(store, first, second, before, scenario, catalog=choice_catalog)
    original = raw(store)
    assert store.collection.album_id(first) == store.collection.album_id(second)
    assert_release(store, NOW + 1, 3, START + 2 * DAY + 1)
    assert raw(store) == original


@pytest.mark.parametrize('first', [OLD, ALIAS])
@pytest.mark.parametrize('before', [True, False])
def test_withdrawal_keeps_a_shared_date_until_its_last_choice_is_withdrawn(store, first, before, choice_catalog):
    second = ALIAS if first == OLD else OLD
    events = setup_choices(store, first, second, before, 'overlap', catalog=choice_catalog)
    assert store.collection.album_id(first) == store.collection.album_id(second)
    original, files = raw(store)
    assert_release(store, NOW + 1, 3, START + 2 * DAY + 1)
    assert raw(store) == (original, files)
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
def test_distinct_editions_keep_separate_choice_dates(store, first, scenario, choice_catalog):
    second = ALIAS if first == OLD else OLD
    setup_choices(store, first, second, False, scenario, catalog=choice_catalog, distinct=True)
    assert store.collection.album_id(first) != store.collection.album_id(second)
    contexts, tastes = measure(store, NOW + 1)
    assert {row['option_id']: row['days'] for row in contexts} == (
        {first: 2, second: 1} if scenario == 'split' else {first: 3, second: 3})
    assert all(sum(source['kind'] == 'albums' for source in taste.sources) == 2 for taste in tastes)
