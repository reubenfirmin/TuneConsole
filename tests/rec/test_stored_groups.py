"""Adaptive Home follows indexed memberships without copying global candidate bags."""
from copy import deepcopy
from contextlib import closing
from tests.derived_rating_support import seed_dislike
from dataclasses import replace
import json

import pytest

from tests.conftest import seed_catalog_track
from tests.group_support import publish as publish_groups, recordings
from tests.legacy_recommendation_fixture_support import recommendation_predecessor, recommendations
from tests.repos.test_typed_recommendation_publication import (
    plan,
)
from tests.rec.test_stored_fresh import fresh
from tests.rec.test_stored_home import publish_bundle, candidates
from tests.store_fixture_support import native_sql_calls, reuse_reporting_reader
from tests.test_group_learning import compiled, menu
from yt_playlist.rec import choice_evidence, group_discovery, group_home, group_learning, group_serving, group_versions, home_vibe, musical_profiles, stored_groups
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Synchronous adaptive serving does not construct the web app."""


@pytest.fixture(autouse=True)
def prepared_vibe_access():
    """Ranking consumes an unlocked control; journey access has its own owners."""
    with home_vibe.access_policy(lambda _store, _now: True):
        yield


@pytest.fixture
def group_assembly_reads(recommendations, monkeypatch):
    """Keep real reporting SQL below synchronous preview/assembly comparisons."""
    with reuse_reporting_reader(recommendations, monkeypatch):
        yield


def forbid_bundle(monkeypatch):
    original = RecommendationProposals._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool'), 'Decoded the retired global candidate bundle'
        return original(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
    monkeypatch.setattr(RecommendationProposals, 'group_pools_json', lambda *args: pytest.fail('Decoded group pools'))


@pytest.mark.parametrize('preset', ['', 'energize'])
@pytest.mark.parametrize('budgeted', [False, True])
def test_real_adaptive_preview_and_choice_preserve_complete_legacy_membership_and_ranking(
        recommendations, group_assembly_reads, monkeypatch, preset, budgeted):
    store = recommendations
    policy = plan(store).policy_version
    size = 160 if budgeted else 32
    records = recordings([('House', size, .1), ('Jazz', size, .9)])
    items = []
    for index, record in enumerate(records):
        title, artist, album, video = f'Song {index}', f'Artist {index // 2}', f'Album {index // 2}', f'v{index}'
        seed_catalog_track(store, record.key, title, artist, video_id=video, genre=record.genre, album=album)
        items.append({'key': record.key, 'title': title, 'artist': artist, 'album': album,
                      'video_id': video, 'thumbnail': '', 'genre': record.genre, 'year': 2000,
                      'pool_policy': policy})
    with store.conn:
        store.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)', (
            records[0].key, json.dumps(musical_profiles.resolve([], genre=records[0].genre)),
            musical_profiles.VERSION, 'preview-fixture', 1))

    def bookkeeping(at):
        with store.conn:
            assert store.conn.execute('UPDATE recordings SET first_enriched_at=?, last_enriched_at=? '
                                      'WHERE identity_key=?', (at, at, records[0].key)).rowcount == 1
            assert store.conn.execute('UPDATE rec_musical_profiles SET prepared_at=? WHERE identity_key=?',
                                      (at, records[0].key)).rowcount == 1

    head = publish_groups(store, records)
    hidden_policy = replace(group_discovery.Policy(), max_depth=2)
    hidden = group_versions.prepare(group_discovery.discover(records, policy=hidden_policy), records,
        head, store.modes.list_modes(False), now=1050, serial=store.taste_groups.serial(),
        input_fingerprint='hidden-preview-replacement', policy=hidden_policy,
        legacy_versions=store.taste_groups.legacy_versions())

    def stage_hidden():
        store.taste_groups.stage(hidden)
        assert store.taste_groups.head_generation() == head['generation']
        assert store.conn.execute('SELECT 1 FROM group_builds').fetchone()

    def clean_hidden():
        store.taste_groups.abandon_staged(hidden)
        while store.taste_groups.cleanup_builds():
            pass
        assert not store.conn.execute('SELECT 1 FROM group_builds').fetchone()
        assert store.taste_groups.head_generation() == head['generation']

    value = {'all': {lane: deepcopy(items) for lane in ('wheelhouse', 'explore', 'comfort')},
             '_meta': {'comfort_pool': len(items), 'full_pools': True}}
    generation = publish_bundle(store, value)
    visible_group = next(group['group_id'] for group in head['groups'] if group.get('leaf'))
    unanswered = menu(store, head, [visible_group], at=900, oid='preview-unanswered-first')
    assert choice_evidence.respond(store, unanswered, 'visible', 901, visible=[visible_group])
    learning, _ = compiled(store, head, 1000)
    store.rec_proposals.put_proposals('group_choice_bandit', learning, 1000)
    if preset:
        store.settings.set_setting(home_vibe.SETTING, preset)
    assert home_vibe.current(store, 1200) == preset
    store._recommendation_files = False
    try:
        store.rec_proposals.put_proposals('mode_bundles', value, 1050)
        expected_preview = group_serving.calculate(store, head, 1100)
        store.rec_proposals.put_proposals(group_serving.SURFACE, expected_preview, 1100)
        expected = group_home.assemble(store, 1200, 0, group_home.inputs(store, 1200))
    finally:
        store._recommendation_files = True
    forbid_bundle(monkeypatch)
    page = RecommendationPublications.candidate_page
    reads, remaining = [], [1]

    def bounded_page(owner, *args, **kwargs):
        from yt_playlist.core.activity import WorkBudgetExceeded
        if budgeted and remaining[0] == 0:
            raise WorkBudgetExceeded('Controlled page admission boundary')
        result = page(owner, *args, **kwargs)
        remaining[0] -= 1
        reads.append((kwargs['group_version'], args[2], kwargs['after']))
        return result

    monkeypatch.setattr(RecommendationPublications, 'candidate_page', bounded_page)
    before = group_serving.view(store)
    turns, partial_frame = 0, False
    while True:
        remaining[0] = 1
        turns += 1
        actual_preview = group_serving.prepare(store, 1100)
        if actual_preview is not None:
            break
        assert group_serving.view(store) == before
        pending = store._group_preview_preparation.pending
        assert pending.guard._reader_released
        partial_frame |= any(state.after >= 0 and not state.exhausted
                             for state in pending.progress['frames'].values())
        bookkeeping(turns + 1)
        assert pending.current(1100), 'Timestamp-only bookkeeping discarded retained preview pages'
        if turns == 1:
            stage_hidden()
            assert pending.current(1100), 'Unpublished group staging discarded retained preview pages'
        elif turns == 2:
            clean_hidden()
            assert pending.current(1100), 'Unrelated group cleanup discarded retained preview pages'
        if turns in (1, 2):
            allocation = group_learning.current_allocation(store, head, 1100 + turns)
            store.rec_proposals.put_proposals('group_choice_bandit', {**learning, 'at': 1100 + turns}, 1100 + turns)
            assert group_learning.current_allocation(store, head, 1100 + turns) == allocation
            assert pending.current(1100), 'Equivalent learning refresh discarded completed pages'
            unanswered = menu(store, head, [visible_group], at=1000 + 3 * turns,
                              oid=f'preview-unanswered-{turns}')
            assert choice_evidence.respond(store, unanswered, 'visible', 1001 + 3 * turns, visible=[visible_group])
            refreshed, _ = compiled(store, head, 1002 + 3 * turns)
            assert refreshed['offered'] == learning['offered'] + turns
            assert refreshed['expires_at'] == learning['expires_at']
            store.rec_proposals.put_proposals('group_choice_bandit', refreshed, 1002 + 3 * turns)
            assert store.rec_proposals.get_proposals('group_choice_bandit') == refreshed
            assert group_learning.current_allocation(store, head, 1100 + turns) == allocation
            assert pending.current(1100), 'Unanswered menu discarded completed candidate pages'
            assert not store.rec_proposals.preview_work()['invalid'], 'Bookkeeping invalidated durable preview pages'
            del store._group_preview_preparation
        assert turns < 100
    assert len(reads) == len(set(reads)), 'Repeated a completed preview page'
    if budgeted:
        assert turns > 1 and partial_frame
        assert any(after >= 0 for _version, _lane, after in reads), 'Never resumed within a candidate frame'
    # Timestamp-only enrichment and profile preparation leave completed previews
    # current, with no candidate SQL or publication on the following worker turn.
    bookkeeping(turns + 2)
    stage_hidden()
    assert not group_serving.due(store, 1101), 'Unpublished group staging invalidated a completed preview'
    clean_hidden()
    assert not group_serving.due(store, 1101), 'Unrelated group cleanup invalidated a completed preview'
    # Narrow history guards still protect mutable file locations belonging to
    # the selected content. Published membership itself is sealed by SQLite.
    for mutation in (
        'UPDATE group_generations SET metadata_offset=metadata_offset+1 WHERE id='
        '(SELECT generation_id FROM group_head WHERE id=1)',
        'UPDATE group_versions SET definition_offset=definition_offset+1 WHERE id='
        '(SELECT group_version_id FROM generation_groups WHERE generation_id='
        '(SELECT generation_id FROM group_head WHERE id=1) LIMIT 1)',
        'UPDATE recommendation_policies SET configuration_offset=configuration_offset+1 WHERE version='
        '(SELECT g.policy_version FROM group_generations g JOIN group_head h ON h.generation_id=g.id)',
    ):
        store.conn.execute('BEGIN')
        try:
            assert store.conn.execute(mutation).rowcount > 0
            assert group_serving.due(store, 1101), mutation
        finally:
            store.conn.rollback()
        assert not group_serving.due(store, 1101), 'Rolled-back content change invalidated the preview'
    read_count, changes = len(reads), store.conn.total_changes
    assert not group_serving.due(store, 1101)
    assert group_serving.prepare(store, 1101) == actual_preview
    assert len(reads) == read_count and store.conn.total_changes == changes
    monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
    assert actual_preview['recommendation_generations'] == [generation]
    assert actual_preview['menus'] == expected_preview['menus']
    for old, new in zip(expected_preview['groups'], actual_preview['groups']):
        for lane, frame in new['frames'].items():
            assert 'keys' not in frame
            assert frame == {key: item for key, item in old['frames'][lane].items() if key != 'keys'}
    actual = group_home.assemble(store, 1200, 0, group_home.inputs(store, 1200))
    signature = lambda cards: [(card['lane'], card['group_version'], [track['key'] for track in card['tracks']])
                               for card in cards]
    assert actual and signature(actual) == signature(expected)
    assert {track['serving_context']['recommendation_generation_id'] for card in actual
            for track in card['tracks']} == {generation}
    # A new global head cannot change an already published adaptive preview's
    # candidate generation or framing before its own replacement is ready.
    replacement = deepcopy(value)
    replacement['all']['wheelhouse'].reverse()
    replacement['_meta']['comfort_pool'] = 0
    publish_bundle(store, replacement, key='replacement-home')
    assert signature(group_home.assemble(store, 1200, 0, group_home.inputs(store, 1200))) == signature(actual)
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()
    assert group_serving.due(store, 1201), 'New recommendation head did not invalidate the preview'
    # Relevant feedback invalidates a retained partial calculation. Rebuilding
    # must remove the suppressed song rather than publishing mixed inputs.
    if budgeted:
        monkeypatch.setattr(RecommendationPublications, 'candidate_page', bounded_page)
        remaining[0] = 1
        assert group_serving.prepare(store, 1201) is None
        pending = store._group_preview_preparation.pending
        seed_dislike(store, items[0]['key'], 1300, 1202)
        assert not pending.current(1203)
        assert store.rec_proposals.preview_work()['invalid']
        del store._group_preview_preparation
        monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
        updated = group_serving.prepare(store, 1203)
        assert updated and all(items[0]['key'] not in {t['key'] for t in frame['tracks']}
                              for group in updated['groups'] for frame in group['frames'].values())
        assert not group_serving.due(store, 1299)
        assert group_serving.due(store, 1300), 'Suppression expiry must admit a new preview'
        from yt_playlist.core.activity import WorkClosing, cancellable_work
        before = group_serving.view(store)
        completed_pages, stopping = [], [False]
        with monkeypatch.context() as cancellation:
            def counted(owner, *args, **kwargs):
                result = page(owner, *args, **kwargs)
                completed_pages.append((kwargs['group_version'], args[2], kwargs['after']))
                return result

            exclusions = stored_groups.home_exclusions

            def stop_after_reads(*args, **kwargs):
                blocked = exclusions(*args, **kwargs)
                stopping[0] = True
                return blocked

            cancellation.setattr(RecommendationPublications, 'candidate_page', counted)
            cancellation.setattr(stored_groups, 'home_exclusions', stop_after_reads)
            with pytest.raises(WorkClosing):
                with cancellable_work(lambda: stopping[0]):
                    group_serving.prepare(store, 1300)
            assert len(completed_pages) == 1
            assert store._group_preview_preparation.pending is None
            assert group_serving.view(store) == before
            assert store.rec_proposals.preview_work()['pages']
            # A genuine allocation change during shutdown must be applied when
            # resuming, without rereading already completed eligible candidates.
            changed = {**learning, 'at': 1301, 'rows': [
                {**row, 'estimate': .9 if row['group_id'] == visible_group else row['estimate']}
                for row in learning['rows']]}
            store.rec_proposals.put_proposals('group_choice_bandit', changed, 1301)
            assert not store.rec_proposals.preview_work()['invalid']
            del store._group_preview_preparation
            resumed = Store(store._db_path)
            with closing(resumed.conn):
                # This fixture's explicitly applied v70/v73 owners do not form
                # a startup history. Reopen their real installed SQL directly.
                resumed._schema_compatibility = True
                resumed._bind_repositories()
                resumed._snapshot_revisions = Revisions(resumed.conn)
                restored = group_serving.prepare(resumed, 1301)
            assert len(completed_pages) == len(set(completed_pages)), 'Repeated a completed page after shutdown/reopen'
        assert restored and any(items[0]['key'] in {t['key'] for t in frame['tracks']}
                                for group in restored['groups'] for frame in group['frames'].values())
        allocation = group_learning.allocation(head, changed)
        assert {group['group_id']: group['allocation'] for group in restored['groups']} == {
            group['group_id']: allocation.get(group['group_id'], 0.) for group in head['groups'] if not group.get('legacy')}
    # Publishing the staged replacement must invalidate the old head, preserving
    # its last-good preview until calculation of the new hierarchy completes.
    before = group_serving.view(store)
    store.taste_groups.publish(hidden)
    assert store.taste_groups.head_generation() != head['generation']
    assert group_serving.due(store, 1302)
    assert group_serving.view(store)['generation'] == before['generation']
    learned_head = store.taste_groups.head(members=False)
    stable_learning = group_learning.compile(store, {}, [], 1302, learned_head)
    store.rec_proposals.put_proposals('group_choice_bandit', stable_learning, 1302)
    assert group_serving.prepare(store, 1302)['generation'] == hidden['generation']
    assert not group_serving.due(store, 1302)
    # Meaningful catalog changes still invalidate the preview. Musical profile
    # changes reach this projection through the immutable group head.
    before = group_serving.view(store)
    with store.conn:
        store.conn.execute("UPDATE recordings SET mb_recording_id='changed-recording' WHERE identity_key=?",
                           (records[0].key,))
    assert group_serving.due(store, 1303)
    assert group_serving.view(store) == before
    assert group_serving.prepare(store, 1303) is not None
    assert not group_serving.due(store, 1303)
    # The allocation itself must still follow genuine learned changes, even
    # when a caller repeats the same state-version identity.
    learned = group_learning.compile(store, {}, [], 1304, learned_head)
    assert learned['state_version'] == stable_learning['state_version']
    selected = next(group['group_id'] for group in learned_head['groups'] if group.get('leaf') and not group.get('legacy'))
    learned['rows'] = [{**row, 'estimate': .9 if row['group_id'] == selected else .5}
                       for row in learned['rows']]
    expected = group_learning.allocation(learned_head, learned)
    assert expected != group_learning.current_allocation(store, learned_head, 1304)
    before = group_serving.view(store)
    store.rec_proposals.put_proposals('group_choice_bandit', learned, 1304)
    assert group_serving.due(store, 1304)
    assert not store.rec_proposals.preview_work()['invalid']
    assert group_serving.view(store) == before
    monkeypatch.setattr(RecommendationPublications, 'candidate_page',
        lambda *args, **kwargs: pytest.fail('Learning-only change repeated candidate preparation'))
    updated = group_serving.prepare(store, 1304)
    assert {group['group_id']: group['allocation'] for group in updated['groups']} == {
        group['group_id']: expected.get(group['group_id'], 0.) for group in learned_head['groups'] if not group.get('legacy')}
    assert not group_serving.due(store, 1304)
    if budgeted and not preset:
        # An arrival after composition must not publish stale allocation or
        # discard the independently completed candidate pages.
        original_compose = group_serving.compose
        latest = {**learned, 'at': 1305, 'rows': [
            {**row, 'estimate': .1 if row['group_id'] == selected else .5}
            for row in learned['rows']]}
        arrivals = []

        def arriving_learning(*args, **kwargs):
            result = original_compose(*args, **kwargs)
            if not arrivals:
                store.rec_proposals.put_proposals('group_choice_bandit', latest, 1305)
                arrivals.append(True)
            return result

        store.rec_proposals.put_proposals('group_choice_bandit', {**learned, 'expires_at': 1305}, 1304)
        with monkeypatch.context() as publication:
            publication.setattr(group_serving, 'compose', arriving_learning)
            assert group_serving.prepare(store, 1305) is None
        assert store.rec_proposals.get_proposals(group_serving.SURFACE) == updated
        assert store._group_preview_preparation.pending.current(1305)
        assert not store.rec_proposals.preview_work()['invalid']
        updated = group_serving.prepare(store, 1305)
        expected = group_learning.allocation(learned_head, latest)
        assert {group['group_id']: group['allocation'] for group in updated['groups']} == {
            group['group_id']: expected.get(group['group_id'], 0.)
            for group in learned_head['groups'] if not group.get('legacy')}
        assert not group_serving.due(store, 1305)


def test_rare_group_seeks_its_complete_tail_without_hydrating_unrelated_candidates(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("INSERT INTO library_memberships SELECT entry_id,'import',5 FROM discovery_memberships")
        store.conn.execute("UPDATE youtube_items SET album='' ")
    records = [replace(record, key=f'fresh-{index + 248}')
               for index, record in enumerate(recordings([('Jazz', 12, .4)]))]
    head = publish_groups(store, records)
    values = candidates(store)
    generation = publish_bundle(store, {'all': {'explore': values}, '_meta': {'full_pools': True}})
    version = head['groups'][0]['version']
    original, reads = RecommendationPublications.candidate_page, []
    def page(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        reads.append((kwargs.get('group_version'), len(result['tracks']), kwargs['limit']))
        return result
    monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
    forbid_bundle(monkeypatch)
    with stored_groups.selection(store, generation, 1100) as selected:
        tracks = selected.tracks(version, 'explore', 0, (), (), set(), 2, 2, {})
    assert [track['key'] for track in tracks] == [f'fresh-{index}' for index in range(248, 260)]
    assert reads == [(version, 12, 128)]
    seed_dislike(store, 'fresh-248', None, 1101)
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE recording_id=252")
    with stored_groups.selection(store, generation, 1102) as selected:
        tracks = selected.tracks(version, 'explore', 0, (), (), set(), 2, 2, {})
    assert len(tracks) == 10
    assert not {'fresh-248', 'fresh-249'} & {track['key'] for track in tracks}
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()


def test_failed_group_selection_releases_its_publication_root(recommendations):
    store = recommendations
    generation = publish_bundle(store, {'all': {}, '_meta': {'unmodeled': True}})
    with pytest.raises(RuntimeError, match='controlled'):
        with stored_groups.selection(store, generation, 10):
            raise RuntimeError('controlled selection failure')
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()
