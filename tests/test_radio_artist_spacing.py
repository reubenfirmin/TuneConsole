"""Radio keeps artists apart across heard history and every proposed stream."""
import numpy as np
import pytest

from yt_playlist.controllers.genres import Genres
from yt_playlist.rec import radio_tuning
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.prepared_fixture_support import prepared_template
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader
from yt_playlist.core.store import Store
from yt_playlist.util.matching import identity_key


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Candidate/SQL checks do not construct the web application."""


@pytest.fixture(scope='module')
def spacing_schema():
    with bounded_consumer_schema((('ambient', 'Ambient'),)) as schema:
        yield schema


def test_artist_spacing_schema_keeps_empty_current_owners_and_snapshot_guards(spacing_schema):
    assert spacing_schema.execute('SELECT count(*) FROM recordings').fetchone()[0] == 0
    assert spacing_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert spacing_schema._revision_script


@pytest.fixture
def store(spacing_schema, tmp_path, monkeypatch):
    # Spacing consumes current catalog reads. Reader admission/copying has its
    # own owners; keep the complete repeated-artist population in private WAL SQL.
    source = Store(tmp_path / 'spacing.db')
    try:
        initialize_consumer(source, spacing_schema)
        source.conn.execute('PRAGMA wal_autocheckpoint=0')
        with reuse_reporting_reader(source, monkeypatch):
            yield source
    finally:
        source.conn.close()


@pytest.fixture(autouse=True)
def completed_spacing_genres(spacing_schema, monkeypatch):
    from yt_playlist.core import genre_identity

    # Artist spacing varies history and candidates against one fixed taxonomy.
    # Resolve its actual lower-layer meanings once, keeping the complete
    # candidate population, genre policy and every playback choice real.
    axes = genre_identity.feature_axes(spacing_schema, {'Ambient', 'ambient'})
    axes.update(genre_identity.feature_axes(spacing_schema,
                {axis for pair in axes.values() for axis in pair if axis}))
    original = Genres.feature_axes

    def feature_axes(controller, requested):
        requested = set(requested)
        missing = requested - axes.keys()
        return {**{label: axes[label] for label in requested if label in axes},
                **(original(controller, missing) if missing else {})}

    monkeypatch.setattr(Genres, 'feature_axes', feature_axes)


def prepare(store, groups):
    modes, bundles, pools = [], {}, []
    for mid, artists in enumerate(groups, 1):
        tracks = []
        for i, artist in enumerate(artists):
            video = f"spacing-{mid}-{i}"
            tracks.append(dict(key=identity_key(video, artist), video_id=video,
                               title=video, artist=artist, genre="Ambient"))
        modes.append(dict(mode_id=mid, label="Ambient", families=[["ambient", len(tracks)]],
                          centroid=np.array([1., mid * .01], dtype=np.float32),
                          size=len(tracks), rep_keys=[]))
        bundles[str(mid)] = {"wheelhouse": tracks}
        pools.append(tracks)
    # Spacing consumes completed subjects and metadata. Keep the full repeated
    # artist populations without running provider ingestion for each recording.
    seed_publication_subjects(store, [{**track, 'album': '', 'year': '2002', 'duration_s': 200}
                                     for tracks in pools for track in tracks])
    store.modes.replace_modes(modes, [], 1)
    publish_bundles(store, bundles)
    return pools


@pytest.fixture(scope='module')
def spacing_template(spacing_schema):
    def build(store):
        return prepare(store, [[f'Artist {i % 9}' for i in range(54)]])

    with prepared_template(spacing_schema, build) as template:
        yield template


def test_artist_spacing_input_keeps_all_repeated_artists_and_published_candidates(spacing_template):
    tracks, = spacing_template.result
    assert len(tracks) == 54
    assert len({track['artist'] for track in tracks}) == 9
    source = spacing_template.source
    assert source.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 54
    assert len(source.modes.list_modes()) == 1
    assert source.recommendations.head('home')
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


@pytest.fixture
def spacing_library(store, spacing_template):
    return spacing_template.restore(store)


def candidates(store, **controls):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, **controls})
    result.rng.seed(387)
    return result


def assert_spaced(tracks):
    for i, track in enumerate(tracks):
        previous = {t["artist"].strip().casefold() for t in tracks[max(0, i - 6):i]}
        assert track["artist"].strip().casefold() not in previous


@pytest.mark.parametrize("theme,comfort", [(.5, .5), (.5, 1), (1, 1)])
def test_artist_does_not_repeat_within_six_songs_even_when_highly_ranked(store, spacing_library, theme, comfort):
    tracks, = spacing_library
    c = candidates(store, theme=theme, comfort=comfort)
    history = [tracks[0]]
    for _ in range(24):
        history.append(c.next_track(history, target=tracks[0]))
    assert_spaced(history)


def test_each_direction_spaces_artists_against_history_and_its_own_prefix(store):
    tracks, = prepare(store, [[f"Artist {i % 12}" for i in range(84)]])
    c = candidates(store)
    history = tracks[:6]
    directions = RadioDirections(c, history[-1])
    directions.update(history, [], 0)
    assert len(directions.slots) == 4
    for branch in directions.slots:
        assert len(branch["tracks"]) == 6
        assert_spaced(history + branch["tracks"])


@pytest.mark.parametrize("anchor", [False, True])
def test_recent_only_local_pool_checks_prepared_neighbours_before_repeating(store, anchor):
    local, neighbour = prepare(store, [["Cliff Martinez"] * 8, ["Floating Points"] * 8])
    c = candidates(store)
    history = [local[0]]
    if anchor:
        pick = c.direction_anchor(history, [neighbour[0]])
    else:
        pick = c.next_track(history, target=local[0])
    assert pick["artist"] == "Floating Points"
    assert len(c.loaded) <= 2


@pytest.mark.parametrize("theme", [.5, 1])
def test_thin_pool_alternates_artists_instead_of_stopping_or_favouring_recent_one(store, theme):
    tracks, = prepare(store, [["Cliff Martinez"] * 10 + ["M83"] * 10])
    c = candidates(store, theme=theme, comfort=1)
    history = [tracks[0], tracks[10]]
    for _ in range(8):
        pick = c.next_track(history, target=history[-1])
        assert pick and pick["artist"] != history[-1]["artist"]
        history.append(pick)
    assert len({t["video_id"] for t in history}) == len(history)


def test_artist_spacing_normalizes_case_and_whitespace(store):
    tracks, = prepare(store, [["Cliff Martinez", "  CLIFF   MARTINEZ ", "M83"]])
    c = candidates(store)
    assert c.next_track([tracks[0]])["artist"] == "M83"
    assert c.next_track([tracks[0], tracks[2]])["video_id"] == tracks[1]["video_id"]


def test_single_artist_can_continue_when_there_is_no_alternative(store):
    tracks, = prepare(store, [["Cliff Martinez"] * 3])
    c = candidates(store, theme=1, comfort=1)
    history = [tracks[0]]
    for _ in range(2):
        history.append(c.next_track(history))
    assert c.next_track(history) is None
    assert len({t["video_id"] for t in history}) == 3
