"""Genre endpoints can reach beyond nearby track groups and remain reversible during playback."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.conftest import seed_catalog_track
from tests.genre_catalog_support import bound_shipped_catalog
from tests.group_support import publish, recordings
from tests.musical_support import publish_intensities
from tests.radio_session_support import controlled_radio_checkpoints, poc, play
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, native_sql_calls, without_query_timing,
)
from yt_playlist.core import genre_identity, schema_inspection
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.controllers.genres import Genres
from yt_playlist.rec import embedding_vectors as embed, group_discovery, measurement, mode_inputs
from yt_playlist.rec import musical_character, radio_intensity, radio_tuning
from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT, GENRE_POOL_LIMIT, RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_genres import GenrePolicy, genre
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.repos.radio_catalog import RadioCatalogRepo
from yt_playlist.web import radio_session

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These candidate and controller checks do not construct an HTTP app."""


@pytest.fixture(scope='module', autouse=True)
def isolated_genre_evidence_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('radio-genre-evidence-home')))
        yield


@pytest.fixture
def mixed():
    return False


@pytest.fixture
def contrast_intensity():
    return None


@pytest.fixture
def electronic_intensity():
    return None


@pytest.fixture
def store(tmp_path, genre_schema, monkeypatch, mixed, contrast_intensity, electronic_intensity):
    source = Store(tmp_path / 'radio-genres.db')
    with closing(source.conn), schema_inspection.reuse_tables(source.conn):
        source.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(source, genre_schema, snapshot_guards=False)
        source.conn.execute('PRAGMA wal_autocheckpoint=0')
        # Playback races happen in the native playlist collaborator. Database
        # publication is uncontended below these selection/pinning checks.
        with mock_snapshot_coordinator(source, monkeypatch):
            # Genre selection consumes completed policies. Keep all nine regions,
            # 108 subjects and their actual SQL/files without exporting and
            # restoring a second copy of this private candidate input.
            with monkeypatch.context() as patch:
                patch.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                              ('genre-input', {'schema': 4, 'algorithm': 'genre-input'}))
                source._fixture_genre_pools = seed_genre_pools(source, mixed=mixed,
                    contrast_intensity=contrast_intensity, electronic_intensity=electronic_intensity)
            source._fixture_radio_pool = [track for rows in source._fixture_genre_pools.values() for track in rows]
            yield source


@pytest.fixture(scope='module')
def genre_schema():
    # Keep the shipped aliases and shared Electronic/Synthwave family. The
    # remaining catalog is unrelated to these nine regions and 108 recordings.
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        with pytest.MonkeyPatch.context() as patch:
            bound_shipped_catalog(patch, {'electronic', 'synthwave', 'jazz', 'rock', 'ambient'})
            schema.execute('PRAGMA query_only=OFF')
            with schema:
                genre_identity.seed_catalog(schema)
            schema.execute('PRAGMA query_only=ON')
        yield schema


@pytest.fixture
def genre_pools(store):
    return store._fixture_genre_pools


@pytest.fixture
def genre_evidence(store):
    source = store
    tracks = source._fixture_radio_pool
    labels = {track['genre'] for track in tracks}
    axes = source.genres.feature_axes(labels)
    axes.update(source.genres.feature_axes({axis for pair in axes.values() for axis in pair if axis}))
    return (axes, source.radio_catalog.details(track['video_id'] for track in tracks),
            musical_character.load(source, [track['key'] for track in tracks]))


@pytest.fixture
def prepared_genre_inputs(store, genre_evidence, monkeypatch):
    prepare_genre_controller(store, genre_evidence, monkeypatch)


def prepare_genre_controller(store, genre_evidence, monkeypatch):
    # These scenarios change playback, tuning and candidates against fixed
    # taxonomy. Keep actual selection/pinning with completed lower-layer reads.
    axes, details, profiles = genre_evidence
    original = Genres.feature_axes

    def feature_axes(controller, requested):
        requested = set(requested)
        missing = requested - axes.keys()
        return {**{label: axes[label] for label in requested if label in axes},
                **(original(controller, missing) if missing else {})}

    monkeypatch.setattr(Genres, 'feature_axes', feature_axes)
    # Taxonomy is a completed, fixed input to these controller transitions.
    # Avoid reopening the same prepared axis lookup for every candidate score;
    # the real GenrePolicy and live feedback exclusions still decide eligibility.
    monkeypatch.setattr(PreparedRadio, 'genre_axes', lambda reader, label: axes.get(label, (None, None)))
    # Metadata and musical evidence stay fixed during the native playback race.
    # Supply those completed reads below the real candidate and genre policy;
    # listening, eligibility, scheduling and playlist reconciliation stay live.
    def fixed_details(controller, videos):
        return deepcopy({video: details[video] for video in videos if video in details})

    def fixed_profiles(source, keys):
        return deepcopy({key: profiles[key] for key in keys if key in profiles})

    monkeypatch.setattr(type(store.radio_catalog), 'details', fixed_details)
    monkeypatch.setattr(musical_character, 'load', fixed_profiles)


def seed_genre_pools(store, *, mixed=False, contrast_intensity=None, electronic_intensity=None):
    # Era separates five Electronic leaves under one parent. Synthwave shares
    # their broader family, while Jazz, Rock and Ambient sit outside it.
    genre_years = [('Electronic', year) for year in (1950, 1965, 1980, 1995, 2010)]
    if contrast_intensity is None:
        genre_years += [(genre, 2025) for genre in ('Synthwave', 'Jazz', 'Rock', 'Ambient')]
    cohorts, owned, familiar, fresh, evidence = [], [], [], [], []
    for index, (label, year) in enumerate(genre_years, 1):
        tracks = []
        for i in range(12 if contrast_intensity is None else 13):
            video = f'genre-{index}-{i}'
            key = f'{video}|artist {video}'
            tracks.append(dict(key=key, video_id=video, title=video, artist=f'Artist {video}',
                               genre=label, year=str(year)))
        cohorts.append(tracks)
        library = tracks
        if mixed and label == 'Electronic':
            library = tracks[:8]
            familiar.extend(tracks[:4])
            fresh.extend(tracks[8:])
        owned.extend(library)
        evidence.extend(replace(record, key=track['key'], year=year)
            for record, track in zip(recordings([(label, len(library), .4)]), library))
    seed_publication_subjects(store, [{**track, 'duration_s': 200} for track in [*owned, *fresh]],
                              discoveries=[t['key'] for t in fresh])
    if familiar:
        account = store.accounts.upsert_account('rotation', None, True)
        with atomic_write(store):
            store.conn.executemany('INSERT INTO play_events(account_id,recording_id,video_id,played_at,is_generated) '
                'SELECT ?,id,?,1,0 FROM recordings WHERE identity_key=?',
                [(account, t['video_id'], t['key']) for t in familiar])
    head = publish(store, evidence, now=1, policy=group_discovery.Policy(era_penalty=.14))
    membership = store.taste_groups.leaf_members(head['generation'])
    pools = {membership[tracks[0]['key']]: tracks for tracks in cohorts}
    assert len(pools) == len(cohorts)
    preview = owned
    if contrast_intensity is not None:
        # The group partition predates a genre correction to a deep member.
        # Its card does not contain that song, while Radio indexes all members.
        last = [group['version'] for group in head['groups'] if group['leaf']][-1]
        contrast = pools[last][-1]
        label, reason = store.genres.resolve('Jazz')
        assert reason is None
        intensities = {track['key']: electronic_intensity for track in owned if electronic_intensity is not None}
        intensities[contrast['key']] = contrast_intensity
        with atomic_write(store):
            store.conn.execute('UPDATE recordings SET genre_key=? WHERE identity_key=?', (label, contrast['key']))
        publish_intensities(store, intensities, now=1)
        preview = [track for rows in cohorts for track in rows[:12]]
    bundles = {'all': {'wheelhouse': preview}, '_meta': {'year_cuts': None}}
    mode_inputs._group_bundles(store, 1, store.rec_model.content_generation(), head, bundles)
    radio_intensity.prepare(store, bundles['_radio'], {}, {})
    publish_bundles(store, bundles, now=1, fresh=fresh)
    return pools


def candidates(store, theme, **values):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, "theme": theme, **values})
    result.rng.seed(3)
    return result


def first_track(pools, label='Electronic'):
    return next(tracks[0] for tracks in pools.values() if tracks[0]['genre'] == label)


def electronic_neighbours(store, pools, seed_group):
    electronic = {group for group, tracks in pools.items() if tracks[0]['genre'] == 'Electronic'}
    assert seed_group in electronic and len(electronic) == GENRE_POOL_LIMIT + 1
    parents = {group['parent_version'] for group in store.taste_groups.head()['groups']
               if group['version'] in electronic}
    assert len(parents) == 1 and None not in parents
    return electronic - {seed_group}


@pytest.mark.parametrize('energy,electronic_intensity,contrast_intensity,expect_contrast', [
    pytest.param(.5, .5, .5, True, id='neutral'),
    pytest.param(1, .5, .5, True, id='same-band'),
    pytest.param(1, .896, .894, True, id='high-band-tie'),
    pytest.param(0, .104, .107, True, id='low-band-tie'),
    pytest.param(1, .501, .499, True, id='midpoint-tie'),
    pytest.param(1, .501, .489, False, id='adjacent-weaker-band'),
    pytest.param(1, .5, .1, False, id='worse-band'),
])
def test_eclectic_reaches_same_tier_contrast_beyond_card_previews(
        store, genre_pools, monkeypatch, energy, electronic_intensity, contrast_intensity, expect_contrast):
    seed = first_track(genre_pools)
    labels = store.rec_query.track_genres(rows[-1]['key'] for rows in genre_pools.values())
    (contrast_group, contrast), = [(mid, rows[-1]) for mid, rows in genre_pools.items()
                                  if genre({'genre': labels[rows[-1]['key']]}) == 'jazz']
    monkeypatch.setattr(embed, 'load_content_vectors', lambda *args, **kwargs: pytest.fail('full vector read'))
    with closing(candidates(store, 0, comfort=1, energy=energy)) as reader:
        assert list(reader.cluster_members.values()) == [13] * 5
        # Verify the real published previews without populating the picker cache.
        for mid, rows in genre_pools.items():
            preview = reader.prepared.initial_track_group_pool(mid, set(), set(), comfort=1)
            assert {track['key'] for track in preview} == {track['key'] for track in rows[:12]}
            assert all(genre(track) == 'electronic' for track in preview)
        leaves = [group['version'] for group in store.taste_groups.head()['groups'] if group['leaf']]
        assert leaves[GENRE_POOL_LIMIT:] == [contrast_group]
        band = round(contrast_intensity * radio_intensity.RESOLUTION)
        pool = reader.prepared.tracks('radio', contrast_group, 'membership', allowed=lambda track: True,
                                      band=band, genres=[labels[contrast['key']]])
        assert [(track['key'], genre(track)) for track in pool] == [(contrast['key'], 'jazz')]
        profiles = musical_character.load(store, [seed['key'], contrast['key']])
        assert musical_character.intensity({'musical': profiles[seed['key']]}) == electronic_intensity
        assert musical_character.intensity({'musical': profiles[contrast['key']]}) == contrast_intensity
        preference = radio_intensity.preference(energy, None)
        assert (radio_intensity.priority(contrast_intensity, preference)
                == radio_intensity.priority(electronic_intensity, preference)) == expect_contrast
        loaded = set()
        tracks = reader.prepared.tracks

        def read(surface, mid, purpose, **options):
            if (surface, purpose) == ('radio', 'membership'):
                assert reader.prepared.current_reader is not None
                if options.get('limit', CLUSTER_POOL_LIMIT) > 1:
                    loaded.add(mid)
            return tracks(surface, mid, purpose, **options)

        monkeypatch.setattr(reader.prepared, 'tracks', read)
        pick = reader.next_track([seed], target=seed)
        assert pick and genre(pick) == ('jazz' if expect_contrast else 'electronic')
        if expect_contrast:
            assert pick['key'] == contrast['key']
        assert 0 < len(loaded) <= GENRE_POOL_LIMIT
        loaded.clear()
        anchor = reader.direction_anchor([seed], [])
        assert anchor and genre(anchor) == ('jazz' if expect_contrast else 'electronic')
        if expect_contrast:
            assert anchor['key'] == contrast['key']
        assert len(loaded) <= GENRE_POOL_LIMIT
        loaded.clear()
        excluded = {contrast['video_id']}
        pick = reader.next_track([seed], target=seed, excluded=excluded)
        assert pick and genre(pick) == 'electronic'
        assert len(loaded) <= GENRE_POOL_LIMIT
        loaded.clear()
        anchor = reader.direction_anchor([seed], [], excluded=excluded)
        assert anchor and genre(anchor) == 'electronic'
        assert len(loaded) <= GENRE_POOL_LIMIT


@pytest.mark.parametrize('contrast_intensity,electronic_intensity', [(.48, .52)])
def test_apply_chill_ranks_current_scores_even_without_prepared_bands(
        store, genre_pools, monkeypatch, contrast_intensity, electronic_intensity):
    seed = first_track(genre_pools)
    labels = store.rec_query.track_genres(rows[-1]['key'] for rows in genre_pools.values())
    quiet = next(rows[-1] for rows in genre_pools.values() if genre({'genre': labels[rows[-1]['key']]}) == 'jazz')
    another = next(rows[1] for rows in genre_pools.values() if rows[-1]['key'] != quiet['key'])
    publish_intensities(store, {another['key']: contrast_intensity}, now=2)
    with closing(candidates(store, .5, comfort=1, energy=0)) as reader:
        plan = reader.for_tuning(reader.tuning)
        plan.prepared.has_intensity = False
        monkeypatch.setattr(plan.prepared, 'tracks', lambda *args, **kwargs: pytest.fail('paged group read on Apply'))
        with plan.prepared.planning_read():
            plan.tuning_inputs([seed], 100_000)
            heads = plan.tuned_heads([seed], seed, {seed['video_id']}, {}, mix_position=0)
        assert len({track['key'] for track in heads}) == 4
        assert {track['key'] for track in heads[:2]} == {quiet['key'], another['key']}
        assert [musical_character.intensity(track) for track in heads] == [contrast_intensity] * 2 + [electronic_intensity] * 2


@pytest.mark.parametrize('mixed', [True])
def test_apply_fresh_rank_read_excludes_generated_starts(store, genre_pools, mixed):
    with closing(candidates(store, .5)) as reader:
        rows = reader.prepared.tuning_ranks('fresh', energy=False)
        assert len(rows) > 1 and all(row['_radio_fresh'] for row in rows)
        heard = rows[0]
        account = store.accounts.get_master_account().id
        with atomic_write(store):
            store.conn.execute('INSERT INTO play_events(account_id,recording_id,video_id,played_at,is_generated) '
                               'VALUES (?,?,?,?,1)', (account, heard['recording_id'], heard['video_id'], 2))
        fresh = reader.prepared.tuning_ranks('fresh', energy=False)
        assert {row['key'] for row in fresh} == {row['key'] for row in rows[1:]}


@pytest.mark.parametrize('mixed', [True])
def test_tuned_background_fill_reads_both_lanes_once(store, genre_pools, mixed, monkeypatch):
    seed = first_track(genre_pools)
    with closing(candidates(store, .5, comfort=0, energy=0)) as reader:
        plan = reader.for_tuning(reader.tuning)
        directions = RadioDirections(plan, seed)
        directions.energy_start = 1
        directions.update([seed], [], 0, force=True, depth=1)
        first = [(branch['id'], branch['tracks'][0]['video_id']) for branch in directions.slots]
        reads = []
        original = RadioCatalogRepo.tuning_ranks

        def read(repo, *args, **options):
            rows = original(repo, *args, **options)
            reads.append({row['purpose'] for row in rows})
            return rows

        monkeypatch.setattr(RadioCatalogRepo, 'tuning_ranks', read)
        monkeypatch.setattr(plan.prepared, 'tracks', lambda *args, **kwargs: pytest.fail('paged background candidate read'))
        directions.fill([seed], selected=0)
        assert reads == [{'membership', 'fresh'}]
        assert not directions.filling
        for branch, (branch_id, head) in zip(directions.slots, first, strict=True):
            assert branch['id'] == branch_id and branch['tracks'][0]['video_id'] == head
            assert len(branch['tracks']) == len({track['key'] for track in branch['tracks']}) == 6
            assert all('musical' in track and '_radio_intensity' not in track for track in branch['tracks'])
        assert any(track['lane'] == 'fresh' for branch in directions.slots for track in branch['tracks'][1:])


def test_themed_energy_prefers_full_on_catalog_to_quieter_group_tracks(store, genre_pools):
    seed = first_track(genre_pools)
    full_on = {}
    for index in range(16):
        key = f'catalog recording {index}|catalog artist {index}'
        seed_catalog_track(store, key, f'Catalog recording {index}', f'Catalog artist {index}',
                           video_id=f'catalog-{index}', genre=seed['genre'], duration_s=220)
        full_on[key] = .95
    publish_intensities(store, full_on, now=3)

    with closing(candidates(store, 1, comfort=1, energy=.79)) as reader:
        plan = reader.for_tuning(reader.tuning)
        directions = RadioDirections(plan, seed)
        directions.energy_start = 1
        directions.update([seed], [], 0, force=True, depth=1)
        branch = directions.slots[0]
        first = branch['tracks'][0]['video_id']
        assert branch['tracks'][0]['key'] in full_on
        directions.fill([seed], selected=0)

        tracks = branch['tracks']
        assert tracks[0]['video_id'] == first
        assert len({track['key'] for track in tracks}) == 6
        assert {track['genre'] for track in tracks} == {seed['genre']}
        assert [musical_character.intensity(track) for track in tracks] == [.95, .95, .5, .95, .95, .95]


def test_automatic_continuation_reads_current_intensity_for_pinned_groups(store, genre_pools, monkeypatch):
    seed = first_track(genre_pools)
    with closing(candidates(store, 1, comfort=.28, energy=.79)) as reader:
        publication = reader.prepared.generation
        order = reader._ordered_track_groups(reader.cluster_for(seed))
        farther = [group for group in order if genre(genre_pools[group][0]) == 'electronic'][-1]
        measured = genre_pools[farther][0]
        assert reader.prepared.has_intensity and measured['key'] != seed['key']
        publish_intensities(store, {measured['key']: .97}, now=2)

        reads = []
        ranks = RadioCatalogRepo.tuning_ranks

        def read(repo, *args, **options):
            reads.append(options['pinned_genre'])
            return ranks(repo, *args, **options)

        monkeypatch.setattr(RadioCatalogRepo, 'tuning_ranks', read)
        directions = RadioDirections(reader, seed)
        directions.energy_start = 1
        history = [seed]
        with reader.prepared.planning_read():
            tracks = directions.continue_sequence(history, [], seed, (), depth=6)
        assert len(reads) == 1
        assert reader.prepared.generation == publication
        assert history == [seed]
        assert len({track['key'] for track in tracks}) == 6
        assert tracks[0]['key'] == measured['key']
        assert musical_character.intensity(tracks[0]) == .97


@pytest.mark.parametrize("energy", [0, .5, 1])
def test_eclectic_actively_changes_genre_beyond_nearest_clusters(
        store, genre_pools, monkeypatch, energy, prepared_genre_inputs):
    def forbidden(*args, **kwargs):
        pytest.fail("Playback selection must not load the full library embedding matrix")

    monkeypatch.setattr(embed, "load_content_vectors", forbidden)
    with closing(candidates(store, 0, comfort=1, energy=energy)) as reader:
        verify_eclectic_genres(reader, genre_pools, monkeypatch)


def verify_eclectic_genres(c, genre_pools, monkeypatch):
    seed = first_track(genre_pools)
    seed_group = next(iter(genre_pools))
    assert c.cluster_for(seed) == seed_group
    assert set(c.cluster_members) == set(genre_pools)
    assert c.prepared.has_intensity
    nearby = electronic_neighbours(c.store, genre_pools, seed_group)
    loaded, scored = [], []
    load, intensity_pool, score = c._load, c.prepared.intensity_pool, c._score

    def loading(mid):
        loaded.append(mid)
        return load(mid)

    def intensity_loading(mid, energy, **kwargs):
        if kwargs.get('limit', CLUSTER_POOL_LIMIT) > 1:
            loaded.append(mid)
        return intensity_pool(mid, energy, **kwargs)

    def scoring(track, *args):
        scored.append(track["video_id"])
        return score(track, *args)

    monkeypatch.setattr(c, "_load", loading)
    monkeypatch.setattr(c.prepared, 'intensity_pool', intensity_loading)
    monkeypatch.setattr(c, "_score", scoring)
    history = [seed]
    for index in range(6):
        loaded.clear()
        scored.clear()
        pick = c.next_track(history, target=seed)
        assert pick and genre(pick) != genre(history[-1])
        assert c.cluster_for(pick) in genre_pools
        assert len(loaded) <= GENRE_POOL_LIMIT
        if index == 0:
            assert loaded
        assert 0 < len(scored) <= GENRE_POOL_LIMIT * CLUSTER_POOL_LIMIT
        history.append(pick)
    assert any(c.cluster_for(track) not in nearby | {seed_group} for track in history[1:])
    assert len({genre(t) for t in history}) >= 4
    assert len({t["video_id"] for t in history}) == len(history)


@pytest.mark.parametrize('mixed', [False, True])
def test_middle_favors_the_current_track_group(store, genre_pools, mixed):
    expected = {t['key']: 'unplayed' for rows in genre_pools.values() for t in rows}
    if mixed:
        local = [rows for rows in genre_pools.values() if rows[0]['genre'] == 'Electronic']
        familiar = [t for rows in local for t in rows[:4]]
        discoveries = [t for rows in local for t in rows[8:]]
        assert not store.rec_query.tracks_by_keys(t['key'] for t in discoveries)
        expected.update((t['key'], 'familiar') for t in familiar)
        expected.update((t['key'], 'new') for t in discoveries)
    c = candidates(store, .5)
    seed = first_track(genre_pools)
    seed_group = next(iter(genre_pools))
    history = [seed]
    share = Counter({'familiar': 2, 'unplayed': 2, 'new': 2} if mixed else {'unplayed': 6})
    try:
        assert c.cluster_for(seed) == seed_group
        assert set(c.cluster_members) == set(genre_pools)
        for _ in range(6):
            pick = c.next_track(history, target=seed)
            assert pick and pick['genre'] == 'Electronic'
            history.append(pick)
        plan = RadioDirections(c, seed)
        assert plan.update([seed], [], 0)
        for tracks in [history[1:], *(branch['tracks'] for branch in plan.slots)]:
            assert len({t['video_id'] for t in tracks}) == 6
            assert all(t['genre'] == 'Electronic' for t in tracks)
            assert Counter(expected[t['key']] for t in tracks) == share
            assert all(t['_radio_rotation_slot'] == expected[t['key']] for t in tracks)
        # Midpoint still permits a nearby different genre when the requested
        # group has no same-genre candidates left.
        excluded = {t['video_id'] for rows in genre_pools.values() for t in rows if t['genre'] == 'Electronic'}
        assert c.next_track([seed], target=first_track(genre_pools, 'Synthwave'), excluded=excluded)['genre'] == 'Synthwave'
        neighbours = electronic_neighbours(store, genre_pools, seed_group)
        played = [t for mid, tracks in genre_pools.items() if mid in neighbours for t in tracks] + [seed]
        played_ids = {t['video_id'] for t in played}
        plan = RadioDirections(c, seed)
        assert plan.update(played, [], 0)
        assert len(plan.slots) == 4
        assert plan.slots[0]['cluster_id'] == seed_group
        assert len({branch['cluster_id'] for branch in plan.slots}) == 4
        for branch in plan.slots:
            videos = {t['video_id'] for t in branch['tracks']}
            assert len(branch['tracks']) == len(videos) == 6
            assert not videos & played_ids
            assert all(c.cluster_for(t) == branch['cluster_id'] or
                       (t['_radio_rotation'] == 'new' and t['genre'] == branch['anchor']['genre'])
                       for t in branch['tracks'])
    finally:
        c.close()


def test_themed_uses_observed_genre_even_with_an_old_seed_and_unheard_prefix(store, genre_pools):
    c = candidates(store, 1)
    seed, playing, unheard = (first_track(genre_pools, label) for label in ('Electronic', 'Jazz', 'Rock'))
    pick = c.next_track([seed, playing, unheard], target=seed, current=playing)
    assert pick["genre"] == "Jazz"
    # Genre identity is narrower than the embedding cluster or genre family.
    assert not c.fits(first_track(genre_pools, 'Synthwave'), seed)  # Same family, distinct genre.
    assert not c.fits({**seed, "genre": None}, seed)


def test_themed_exhaustion_does_not_spill_into_other_genres(store, genre_pools):
    c = candidates(store, 1)
    seed = first_track(genre_pools)
    excluded = {t["video_id"] for rows in genre_pools.values() for t in rows if t["genre"] == "Electronic"}
    assert c.next_track([seed], excluded=excluded) is None
    assert c.direction_anchor([seed], [seed], excluded=excluded) is None
    assert c.next_track([{**seed, "genre": None}]) is None


def test_all_themed_branches_follow_a_changed_playing_genre(store, genre_pools):
    c = candidates(store, 1)
    seed, playing = first_track(genre_pools), first_track(genre_pools, 'Jazz')
    plan = RadioDirections(c, seed)
    plan.update([seed], [], 0)
    scheduled = plan.slots[0]["tracks"][:1]
    plan.update([seed, playing], scheduled, 1)
    assert all(branch["tracks"] for branch in plan.slots)
    assert all(t["genre"] == "Jazz" for branch in plan.slots for t in branch["tracks"])
    assert all(t["video_id"] != playing["video_id"] for branch in plan.slots for t in branch["tracks"])
    assert not plan.update([seed, playing], scheduled, 1)


def test_genre_identity_handles_equivalents_without_collapsing_families():
    assert genre({"genre": "DRUM AND BASS"}) == genre({"genre": "Drum & Bass"})
    assert genre({"genre": "Synthwave"}) != genre({"genre": "Electronic"})
    policy = GenrePolicy(0, [{"genre": "Electronic"}])
    assert policy.contrast({"genre": "Jazz"}) > policy.contrast({"genre": "Synthwave"})
    assert policy.contrast({"genre": None}) < policy.contrast({"genre": "Electronic"})


def test_dj_can_alternate_genre_endpoints_without_restarting_or_rejecting_songs(
        controlled_radio_checkpoints, poc, genre_pools, monkeypatch, prepared_genre_inputs):
    c, remote, frames, _, _ = poc
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)
    seed = first_track(genre_pools)
    assert c.start(seed["video_id"])["ok"]
    play(c, remote.ids[0])
    playlist = c.session.playlist
    initial_frames = list(frames)

    def tune(theme):
        assert c.tune({**radio_tuning.DEFAULTS, "theme": theme})["ok"]
        c.advance(c.session)
        assert not c.status()["updating"]
        assert c.session.playlist == playlist
        assert frames == initial_frames

    tune(0)
    first = c.session.tracks[c.session.position + 1]
    assert first["genre"] != "Electronic"
    # The song on air, rather than the seed or the previous slider value,
    # establishes the next genre pin.
    play(c, first["video_id"])
    playing = c.session.history_view().current
    history = list(c.session.heard)
    for theme in (1, 0, 1):
        tune(theme)
        future = c.session.tracks[c.session.position + 1:]
        assert future
        if theme == 1:
            assert all(genre(t) == genre(playing) for t in future)
            assert all(genre(t) == genre(playing) for b in c.session.directions.slots for t in b["tracks"])
        else:
            assert genre(future[0]) != genre(playing)
        assert c.session.heard == history
        assert c.session.observed_video == playing["video_id"]
    assert not c.session.rejected


def test_pending_genre_pin_rebases_after_a_native_song_change(
        controlled_radio_checkpoints, poc, genre_pools, monkeypatch, prepared_genre_inputs):
    c, remote, _, _, _ = poc
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)
    assert c.start(first_track(genre_pools)["video_id"])["ok"]
    play(c, remote.ids[0])
    assert c.tune({**radio_tuning.DEFAULTS, "theme": 0})["ok"]
    c.advance(c.session)
    next_song = c.session.tracks[c.session.position + 1]
    assert next_song["genre"] != "Electronic"
    assert c.tune({**radio_tuning.DEFAULTS, "theme": 1})["ok"]
    # Playback moves while the new future is being added. The transaction must
    # keep the heard prefix and repin its unsent future to the new playing genre.
    def transition():
        c.ctx.bridge.now_playing = {"video_id": next_song["video_id"],
            "playlist_ytm_id": c.session.playlist, "paused": False, "position": 30, "duration": 300}

    remote.after_add = transition
    c.advance(c.session)
    remote.after_add = lambda: None
    c.advance(c.session)
    assert not c.status()["updating"]
    assert c.session.observed_video == next_song["video_id"]
    assert next_song["video_id"] in [t["video_id"] for t in c.session.heard]
    assert all(genre(t) == genre(next_song) for t in c.session.tracks[c.session.position + 1:])
    assert all(genre(t) == genre(next_song) for b in c.session.directions.slots for t in b["tracks"])
    assert not c.session.rejected
