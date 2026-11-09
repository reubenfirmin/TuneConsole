import numpy as np
from contextlib import closing
from time import perf_counter
from types import SimpleNamespace
from tests.derived_rating_support import seed_dislike
from tests.retained_rating_support import apply_model_ratings
import pytest
from tests.conftest import seed_catalog_track
from tests.genre_catalog_support import bound_shipped_catalog
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_bundle_subjects, seed_publication_subjects
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, file_consumer_store as store,
    initialize_consumer, mock_snapshot_coordinator, native_sql_calls,
    reuse_reporting_reader, without_query_timing,
)
from yt_playlist.core import catalog, schema_inspection
from yt_playlist.core.store import Store
from yt_playlist.rec import mode_selection, mode_surfaces as ms, stored_home


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Card assembly does not construct an HTTP application or background jobs."""


def _eye(i, d=4):
    v = np.zeros(d, dtype=np.float32); v[i] = 1.0
    return v


def _publish(store, payload, *, subjects_ready=False):
    if subjects_ready:
        assert _subject_keys(payload) <= store._assembly_subject_keys
    else:
        seed_bundle_subjects(store, payload)
    with store.conn:
        for mode, bucket in payload.items():
            if mode.startswith('_'):
                continue
            for items in bucket.values():
                for item in items:
                    if item.get('year') is not None:
                        entry = store.conn.execute('SELECT id FROM catalog_tracks WHERE identity_key=?',
                                                   (item['key'],)).fetchone()[0]
                        catalog.choose_field(store.conn, entry, 'mb_year', str(item['year']))
    generation = publish_bundles(store, payload, 1)
    assert store.recommendations.head('home')['id'] == generation


def _publication(store):
    return {table: [tuple(row) for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')]
            for table in ('recommendation_heads', 'recommendation_surface_pools', 'recommendation_pools',
                          'recommendation_candidates', 'recommendation_bundle_framing', 'prepared_outputs')}


def _subject_keys(payload):
    return {item['key'] for mode, bucket in payload.items() if not mode.startswith('_')
            for items in bucket.values() for item in items}


def _mode_candidates(n_modes=4, *, candidates_per_pool=20):
    modes = [{"mode_id": m, "label": f"m{m}", "families": [["house", 1]],
              "centroid": _eye(m - 1), "size": 80 - m, "rep_keys": []}
             for m in range(1, n_modes + 1)]
    def items(prefix):
        return [{"key": f"{prefix}{i}", "video_id": f"v{prefix}{i}", "title": f"{prefix}{i}", "artist": f"{prefix}art{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
                for i in range(candidates_per_pool)]
    payload = {str(m): {} for m in range(1, n_modes + 1)}
    for m in range(1, n_modes + 1):
        for surf in ms.CARD_SURFACES:
            payload[str(m)][surf] = items(f"{surf}-m{m}-")
    payload["_meta"] = {"comfort_pool": 100, "year_cuts": None}   # comfort credible -> it holds the 4th slot
    return modes, payload


def _seed(store, n_modes=4, *, candidates_per_pool=20):
    """Seed live modes and subjects; let each consumer publish its final payload."""
    modes, payload = _mode_candidates(n_modes, candidates_per_pool=candidates_per_pool)
    store.modes.replace_modes(modes, retired_ids=[], now=1.0)
    subjects = {item['key']: item for mid in map(str, range(1, n_modes + 1))
                for items in payload[mid].values() for item in items}
    discovered = {item['key'] for mid in map(str, range(1, n_modes + 1))
                  for item in payload[mid]['fresh']}
    # Ingestion/artist inference is below assignment and live dislike scoring.
    # Keep genuine identities, catalog membership and current keyed genres.
    seed_publication_subjects(store, subjects.values(), discoveries=discovered)
    store._assembly_subject_keys = _subject_keys(payload)
    assert not store.recommendations.heads()
    return payload


@pytest.fixture
def ranked_cards():
    """Assignment consumes controlled published ranks, with real scratch SQL.

    The neighboring publication, current-genre and durable-dislike cases own
    application catalog integration. These cases own complete card assignment,
    priority rotation, diversity and cross-card deduplication.
    """
    modes, payload = _mode_candidates()

    class PublishedPools:
        def __init__(self):
            self.rows = {(mid, lane): [{**item, 'pool_rank': rank, 'serving_context': {}}
                                      for rank, item in enumerate(items)]
                         for mid, bucket in payload.items() if not mid.startswith('_')
                         for lane, items in bucket.items()}
            self.ids = {key: index for index, key in enumerate(self.rows, 1)}

        def framing(self, surface, *, generation):
            return {'comfort_pool': payload['_meta']['comfort_pool']}

        def selections(self, surface, lane, *, generation):
            return [{'mode_key': mid, 'id': self.ids[mid, lane]}
                    for mid, purpose in self.rows if purpose == lane]

        def candidate_page(self, surface, mode, lane, *, generation, after, limit):
            rows = self.rows[mode, lane][after + 1:after + 1 + limit]
            return {'tracks': rows, 'after': rows[-1]['pool_rank'] if rows else after,
                    'exhausted': after + 1 + len(rows) == len(self.rows[mode, lane])}

        def selected_candidates(self, surface, selections, *, generation):
            return [[row for row in self.rows[mode, lane] if row['key'] in keys]
                    for mode, lane, keys in selections]

        def pool(self, surface, mode, purpose, *, generation):
            return None

    def assemble(*, epoch=0, order=(1, 2, 3, 4)):
        reader = SimpleNamespace(
            recommendations=PublishedPools(),
            settings=SimpleNamespace(get_setting=lambda key: None),
            modes=SimpleNamespace(list_modes=lambda **kwargs: modes),
            rec_model=SimpleNamespace(muted_artists=lambda: set(),
                disliked_key_page=lambda **kwargs: [], suppressed_keys=lambda *args, **kwargs: set()),
            rec_eligibility=SimpleNamespace(suppressed_candidate_keys=lambda keys, now: set()),
            rec_query=SimpleNamespace(track_genres=lambda keys: {}),
            genres=SimpleNamespace(feature_axes=lambda genres: dict.fromkeys(genres, ('house', None))))
        return stored_home._assemble(reader, 1, 10., epoch, (), (), order, {'leans': {}})

    return payload, assemble


@pytest.fixture
def assembly_subjects(store, assembly_subject_template, record_property):
    started = perf_counter()
    payload = assembly_subject_template.restore(store)
    store._assembly_subject_keys = _subject_keys(payload)
    record_property('subject_restore_seconds', perf_counter() - started)
    assert not store.recommendations.heads()
    assert len(store.modes.list_modes(active_only=True)) == 4
    library = store.rec_query.tracks_by_keys(_subject_keys(payload))
    discovery = store.discovery.discovered_tracks_by_keys(_subject_keys(payload))
    assert set(library) == {row['key'] for mode in map(str, range(1, 5))
                            for lane in ms.CARD_SURFACES if lane != 'fresh'
                            for row in payload[mode][lane]}
    assert set(discovery) == {row['key'] for mode in map(str, range(1, 5))
                              for row in payload[mode]['fresh']}
    return payload


@pytest.fixture(scope='module')
def assembly_subject_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, _seed) as template:
        yield template


@pytest.fixture(autouse=True)
def assembly_costs(monkeypatch, record_property):
    """Separate actual producer, publication and assembly costs for the cohort."""
    from functools import wraps
    import sys
    import time

    costs = {}
    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                values = costs.setdefault(name, [0, 0.0, 0.0])
                values[0] += 1
                values[1] += time.perf_counter() - wall
                values[2] += time.thread_time() - cpu
        return call

    module = sys.modules[__name__]
    for owner, name in ((module, '_seed'), (module, '_publish'), (ms, 'assemble_cards')):
        monkeypatch.setattr(owner, name, timed(name, getattr(owner, name)))
    yield
    for name, (calls, wall, cpu) in costs.items():
        record_property(name + '_calls', calls)
        record_property(name + '_wall_seconds', wall)
        record_property(name + '_thread_cpu_seconds', cpu)


def test_temporal_card_when_comfort_not_credible(store):
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": _eye(0), "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)

    def t(prefix, year):
        return [{"key": f"{prefix}{i}", "video_id": "v", "title": f"{prefix}{i}", "artist": f"{prefix}a{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "temporal",
                 "genre": "house", "year": year} for i in range(8)]
    # mode 1's temporal bucket spans old (1990) and new (2020); comfort empty + not credible
    payload = {"1": {"wheelhouse": [], "explore": [], "fresh": [], "comfort": [],
                     "temporal": t("old", 1990) + t("new", 2020)}}
    payload["_meta"] = {"comfort_pool": 0, "year_cuts": [2000, 2010]}     # comfort not credible
    _publish(store, payload)
    # band 0 (epoch 0) -> Throwback, only the <=2000 tracks
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    assert len(cards) == 1
    c = cards[0]
    assert c["lane"] == "temporal" and c["label"] == "Throwback"
    assert all(t["title"].startswith("old") for t in c["tracks"])
    # band 2 (epoch 2) -> Recent Picks, only the >2010 tracks
    c2 = ms.assemble_cards(store, now=10.0, epoch=2)[0]
    assert c2["label"] == "Recent Picks"
    assert all(t["title"].startswith("new") for t in c2["tracks"])


def test_comfort_not_backfilled(store):
    # Comfort credible (holds the slot) but its mode bucket is thin and there's a fat general pool.
    # Comfort must NOT pad from the general pool, so it stays thin / dropped - never faked.
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": _eye(0), "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)
    thin = [{"key": f"c{i}", "video_id": "v", "title": "t", "artist": f"ca{i}", "album": "",
             "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"} for i in range(2)]
    fat = [{"key": f"g{i}", "video_id": "v", "title": "t", "artist": f"ga{i}", "album": "",
            "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"} for i in range(20)]
    payload = {"1": {"wheelhouse": [], "explore": [], "fresh": [], "comfort": thin},
               "all": {"comfort": fat}, "_meta": {"comfort_pool": 100, "year_cuts": None}}
    _publish(store, payload)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    comfort = [c for c in cards if c["lane"] == "comfort"]
    # 2 thin tracks, < _MIN_CARD, and NO backfill -> comfort is dropped rather than padded from 'all'
    assert comfort == [] or all(not any(t["key"].startswith("g") for t in c["tracks"]) for c in comfort)


def test_comfort_thin_rotates_to_temporal(store):
    # Comfort holds the 4th slot (global pool credible) but its assigned mode's bucket is thin, and the
    # library has year data. Temporal must rotate into the slot so the row stays at four, not drop to 3.
    store.modes.replace_modes(
        [{"mode_id": m, "label": f"m{m}", "families": [["house", 1]],
          "centroid": _eye(m - 1), "size": 80 - m, "rep_keys": []} for m in range(1, 5)],
        retired_ids=[], now=1.0)

    def items(prefix, n, year=None):
        out = []
        for i in range(n):
            d = {"key": f"{prefix}{i}", "video_id": "v", "title": f"{prefix}{i}", "artist": f"{prefix}a{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
            if year is not None:
                d["year"] = year
            out.append(d)
        return out

    # Slots 1-3 render on modes 1-3; mode 4's comfort bucket is thin (2) but its temporal bucket is deep.
    payload = {"1": {"wheelhouse": items("w", 20)},
               "2": {"explore": items("e", 20)},
               "3": {"fresh": items("f", 20)},
               "4": {"comfort": items("c", 2), "temporal": items("t", 8, year=1990)}}
    payload["_meta"] = {"comfort_pool": 100, "year_cuts": [2000, 2010]}   # comfort credible; year data present
    _publish(store, payload)

    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    lanes = [c["lane"] for c in cards]
    assert "comfort" not in lanes                 # thin comfort is not shown
    assert "temporal" in lanes                    # temporal rotated into the 4th slot
    assert len(cards) == 4                         # row stayed at four
    tcard = next(c for c in cards if c["lane"] == "temporal")
    assert tcard["mode_id"] == 4 and all(t["key"].startswith("t") for t in tcard["tracks"])


def test_assemble_four_cards_distinct_modes(ranked_cards):
    _payload, assemble = ranked_cards
    cards = assemble()
    assert len(cards) == 4
    assert len({c["mode_id"] for c in cards}) == 4          # one distinct mode per card
    assert all(c["mode_id"] in (1, 2, 3, 4) for c in cards)
    assert all(len(c["tracks"]) <= 12 for c in cards)
    keys = [t["key"] for c in cards for t in c["tracks"]]
    assert len(keys) == len(set(keys))                      # cross-card dedup
    assert all("mode_id" in c for c in cards)


def test_assemble_empty_without_bundles(store):
    assert ms.assemble_cards(store, now=10.0, epoch=0) == []


@pytest.fixture(scope='module')
def dislike_board_schema():
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as source:
        yield source


@pytest.fixture
def dislike_board_store(dislike_board_schema, tmp_path, monkeypatch):
    from yt_playlist.rec import measurement

    source = Store(tmp_path / 'dislike.db')
    with closing(source.conn), schema_inspection.reuse_tables(source.conn):
        source.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(source, dislike_board_schema, snapshot_guards=False)
        source.conn.execute('PRAGMA wal_autocheckpoint=0')
        payload = _seed(source)
        seed_publication_subjects(source, [{'key': 'disliked-seed', 'title': 'Disliked seed',
            'artist': 'Disliked artist', 'video_id': 'disliked-seed'}])
        keys = sorted(_subject_keys(payload))
        vectors = np.array([[1., 0.] if key.endswith('-0') else [0., 1.] for key in keys] + [[1., 0.]],
                           dtype=np.float32)
        source.rec_model.replace_rec_vectors([(key, vector.tobytes())
            for key, vector in zip(keys + ['disliked-seed'], vectors)])
        payload['_ppr'] = {str(mid): list(dict.fromkeys(item['key'] for surf in ms.CARD_SURFACES
            for item in payload[str(mid)][surf])) for mid in range(1, 5)}
        # There are no concurrent database writers in this ranking check.
        # Keep its actual SQL/vector files, complete pools and publication.
        with mock_snapshot_coordinator(source, monkeypatch), monkeypatch.context() as patch:
            # Policy fingerprint production is below live dislike scoring.
            # Retain real policy documents, candidate publication and files.
            patch.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                          ('dislike-input', {'ranker': 'dislike-input'}))
            _publish(source, payload, subjects_ready=True)
            # Catalog metadata is fixed input to these three ranking passes.
            # Feedback, vector reads, eligibility and published rows stay live.
            facts = source.recommendations._candidate_facts(keys)
            patch.setattr(type(source.recommendations), '_candidate_facts',
                          lambda _controller, requested: {key: facts[key] for key in requested})
            yield source


@pytest.mark.parametrize("ppr_share", [0, 1])
@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_cached_cards_respond_to_durable_dislikes_and_clear_without_rebuild(
        dislike_board_store, monkeypatch, ppr_share):
    from yt_playlist.controllers.genres import Genres
    from yt_playlist.rec import rec_params

    store = dislike_board_store
    # Retain the original four modes and all four 20-track lanes in each mode,
    # with both published rankers and actual durable dislike scoring.
    rec_params.set_param(store, "ppr_ab_share", ppr_share)
    monkeypatch.setattr(mode_selection, "select_modes", lambda *args, **kwargs: [1, 2, 3, 4])
    # The selected themes are controlled inputs; their bandit/history producers
    # have separate owners. Keep current vectors and durable dislike scoring real.
    monkeypatch.setattr(stored_home.mode_eval, "mode_bandit_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr(stored_home.layers, "now_mode_posterior", lambda *args, **kwargs: {})
    monkeypatch.setattr(stored_home.transient, "facet_leans", lambda *args, **kwargs: {})
    # Taxonomy is fixed while the recording ratings change. Current-genre and
    # family-filter cases below retain the actual taxonomy reads.
    axes = store.genres.feature_axes({'house', 'House'})

    def feature_axes(_controller, labels):
        return {label: axes[label] for label in labels}

    monkeypatch.setattr(Genres, 'feature_axes', feature_axes)
    publication = _publication(store)
    before = ms.assemble_cards(store, now=100, epoch=0)
    assert len(before) == 4
    apply_model_ratings(store, {"disliked-seed": "DISLIKE"}, now=1)
    # Outlive the 365-day ban and every transient layer. The prepared payload stays unchanged.
    after = ms.assemble_cards(store, now=1000 * 86400, epoch=0)
    assert len(after) == 4
    assert all(card["tracks"][0]["key"].endswith("-0") for card in before)
    assert all(not card["tracks"][0]["key"].endswith("-0") for card in after)
    apply_model_ratings(store, {"disliked-seed": "INDIFFERENT"}, now=1000 * 86400)
    cleared = ms.assemble_cards(store, now=1000 * 86400, epoch=0)
    assert [[t["key"] for t in c["tracks"]] for c in cleared] == [
        [t["key"] for t in c["tracks"]] for c in before]
    assert _publication(store) == publication


def test_full_buckets_do_not_pin_a_theme_to_one_framing(ranked_cards):
    payload, assemble = ranked_cards
    # One theme has the deepest pool in every framing. All the others can fill a
    # complete card too; raw pool depth must not undo the model's theme rotation.
    for mid in (2, 3, 4):
        for surf in ms.CARD_SURFACES:
            payload[str(mid)][surf] = payload[str(mid)][surf][:12]
    # Assignment must honor every selected priority even when mode 1 has an
    # extra candidate. Check each possible leading theme directly; sampling's
    # posterior and deterministic RNG have independent mode-selection tests.
    order = [1, 2, 3, 4]
    menus = [assemble(epoch=e, order=order[e:] + order[:e]) for e in range(4)]
    wheelhouse = [next(c["mode_id"] for c in cards if c["lane"] == "wheelhouse") for cards in menus]
    assert set(wheelhouse) == {1, 2, 3, 4}
    assert all(len(cards) == 4 for cards in menus)
    assert all(len({c['mode_id'] for c in cards}) == 4 for cards in menus)
    assert all(len(c["tracks"]) == 12 for cards in menus for c in cards)
    for cards in menus:
        keys = [track['key'] for card in cards for track in card['tracks']]
        assert len(keys) == len(set(keys))


def test_logging_this_menu_does_not_change_its_themes_on_rerender(store, assembly_subjects):
    _publish(store, assembly_subjects, subjects_ready=True)
    before = ms.assemble_cards(store, now=10.0, epoch=0)
    store.modes.log_impressions(0, [(c["lane"], c["mode_id"]) for c in before], 10.0)
    store.modes.log_pick(77, before[0]["mode_id"], 11.0)
    assert ms.assemble_cards(store, now=12.0, epoch=0) == before


@pytest.mark.parametrize("restricted_lane", ["comfort", "fresh"])
@pytest.mark.parametrize("available", [4, 12])
def test_assignment_preserves_the_only_mode_that_can_fill_a_lane(store, monkeypatch,
                                                               restricted_lane, available, assembly_subjects):
    payload = assembly_subjects
    # The preferred mode can fill any lane, but is the only source for this one.
    # Spending it on Wheelhouse loses a card, even when four cards are feasible.
    payload["1"][restricted_lane] = payload["1"][restricted_lane][:available]
    for mid in (2, 3, 4):
        payload[str(mid)][restricted_lane] = []
    _publish(store, payload, subjects_ready=True)
    monkeypatch.setattr(mode_selection, "select_modes", lambda *args, **kwargs: [1, 2, 3, 4])

    cards = ms.assemble_cards(store, now=10.0, epoch=0)

    assert len(cards) == 4
    assert len({c["mode_id"] for c in cards}) == 4
    restricted = next(c for c in cards if c["lane"] == restricted_lane)
    assert restricted["mode_id"] == 1
    assert len(restricted["tracks"]) == available


def test_diversify_caps_artist_and_album():
    items = (
        [{"artist": "A", "album": "AlbA", "key": f"a{i}"} for i in range(4)]        # same artist -> cap
        + [{"artist": f"B{i}", "album": "Comp", "key": f"c{i}"} for i in range(4)]  # one album -> cap
        + [{"artist": f"S{i}", "album": "", "key": f"s{i}"} for i in range(3)]      # singles -> uncapped
    )
    out = ms._diversify(items, max_artist=2, max_album=2)
    assert sum(1 for d in out if d["artist"] == "A") == 2
    assert sum(1 for d in out if d["album"] == "Comp") == 2
    assert sum(1 for d in out if d["album"] == "") == 3
    assert [d['key'] for d in out] == ['a0', 'a1', 'c0', 'c1', 's0', 's1', 's2']


def test_thin_bucket_backfilled_from_general_pool(store):
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": _eye(0), "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)
    thin = [{"key": f"m{i}", "video_id": "v", "title": "t", "artist": f"mart{i}",
             "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
            for i in range(2)]                                  # only 2 in the mode bucket
    general = [{"key": f"g{i}", "video_id": "v", "title": "t", "artist": f"gart{i}",
               "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
               for i in range(20)]                              # general backfill pool
    payload = {"1": {}, "all": {}}
    for surf in ms.CARD_SURFACES:
        payload["1"][surf] = list(thin)
        payload["all"][surf] = list(general)
    _publish(store, payload)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    assert len(cards) == 1                                      # one mode -> one card
    card = cards[0]
    assert len(card["tracks"]) == 12                           # backfilled from 2 up to PROTO_SIZE
    keys = {t["key"] for t in card["tracks"]}
    assert {"m0", "m1"} <= keys                                # the mode tracks lead
    assert any(k.startswith("g") for k in keys)                # backfilled from the general pool


def _genre_items(prefix, genre, count):
    return [{"key": f"{prefix}{i}", "video_id": f"v{prefix}{i}", "title": f"{prefix}{i}",
             "artist": f"{prefix} artist {i}", "album": "", "thumbnail": None, "genre": genre}
            for i in range(count)]


@pytest.mark.parametrize("full_pools", [False, True])
def test_thin_mix_exhausts_deeper_family_before_adjacent_tracks(store, monkeypatch, full_pools):
    from yt_playlist.rec import surfaces

    store.modes.replace_modes([
        {"mode_id": 1, "label": "Soul", "families": [["soul-funk", 100]],
         "centroid": _eye(0), "size": 100, "rep_keys": []}], retired_ids=[], now=1.0)
    core = _genre_items("core", "Soul", 6)
    deep = _genre_items("deep", "Funk", 12)
    nearby = _genre_items("near", "Jazz", 20)
    unrelated = _genre_items("far", "Electronic", 20)
    payload = {"1": {"wheelhouse": core},
               "all": {"wheelhouse": unrelated + nearby + deep},
               "_meta": {"full_pools": full_pools}}
    # Current serving traverses the accepted publication even when the producer
    # omitted the old completeness flag; it never ranks a missing tail on request.
    _publish(store, payload)
    publication = _publication(store)
    seed_dislike(store, "deep0", until=None, now=1.0)
    entry = store.conn.execute("SELECT id FROM catalog_tracks WHERE identity_key='deep1'").fetchone()[0]
    identity = store.accounts.upsert_account("test", "cred", None, True)
    playlist = store.playlists.upsert_playlist(identity, "GENERATED", "Generated", 1, "hash", 1)
    store.playlists.set_playlist_tracks(playlist, [entry])
    store.playlists.mark_generated_playlist("GENERATED")

    def ranked_library(*args, **kwargs):
        pytest.fail('Serving must reuse accepted ranks without foreground ranking')

    monkeypatch.setattr(surfaces, "for_you", ranked_library)

    cards = ms.assemble_cards(store, 10.0, 0, include_genres=["soul-funk", "electro-synth"])

    assert len(cards) == 1
    tracks = cards[0]["tracks"]
    assert len(tracks) == len({d["key"] for d in tracks}) == 12
    assert all(d["genre"] in {"Soul", "Funk"} for d in tracks)
    keys = {d["key"] for d in tracks}
    assert "deep0" not in keys  # Explicit dislike still suppresses a candidate.
    assert "deep1" in keys  # Generated membership does not erase independent eligibility.
    assert _publication(store) == publication


@pytest.fixture(scope='module')
def thin_mix_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def thin_mix_store(tmp_path, thin_mix_schema, native_sql_calls, monkeypatch):
    from yt_playlist.core import genre_identity

    # Keep the shipped aliases, family identities and distances for all four
    # genres. Unrelated vocabulary and catalog ingestion are lower inputs.
    bound_shipped_catalog(monkeypatch, ('soul', 'blues', 'techno', 'jazz'))
    with _file_consumer_store(tmp_path, thin_mix_schema) as database:
        with database.conn:
            genre_identity.seed_catalog(database.conn)
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.mark.parametrize("lane", ["wheelhouse", "fresh"])
def test_thin_mix_uses_close_neighbors_but_never_avoided_or_unrelated_genres(thin_mix_store, lane):
    store = thin_mix_store
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Soul", "families": [["soul-funk", 100]],
         "centroid": _eye(0), "size": 100, "rep_keys": []}], retired_ids=[], now=1.0)
    core = _genre_items("core", "Soul", 6)
    nearby = _genre_items("near", "Blues", 20)
    unrelated = _genre_items("far", "Techno", 20)
    avoided = _genre_items("avoid", "Jazz", 20)
    payload = {"1": {lane: core},
               "all": {lane: unrelated + avoided + nearby + core},
               "_meta": {"full_pools": True}}
    subjects = {item['key']: item for item in core + nearby + unrelated + avoided}
    seed_publication_subjects(store, subjects.values(),
                              discoveries=subjects if lane == 'fresh' else ())
    store._assembly_subject_keys = set(subjects)
    _publish(store, payload, subjects_ready=True)

    cards = ms.assemble_cards(store, 10.0, 0, avoid_genres=["jazz"])

    assert len(cards) == 1 and cards[0]["lane"] == lane
    tracks = cards[0]["tracks"]
    assert len(tracks) == len({d["key"] for d in tracks}) == 12
    assert [d["genre"] for d in tracks] == ["Soul"] * 6 + ["Blues"] * 6
    head = store.recommendations.head('home')
    focused = ms.assemble_cards(store, 10.0, 0, include_genres=['soul-funk'], avoid_genres=['jazz'])
    assert len(focused) == 1 and focused[0]['lane'] == lane
    assert [track['genre'] for track in focused[0]['tracks']] == ['Soul'] * 6
    assert store.recommendations.head('home') == head
    with store.conn:
        changed = store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='blues'")
        assert changed.rowcount == 1
    # A new request resolves the changed taxonomy, even for the same accepted
    # pools and raw labels. The explicit avoided family still wins.
    refreshed = ms.assemble_cards(store, 11.0, 0, avoid_genres=['jazz'])
    assert len(refreshed) == 1
    assert [track['genre'] for track in refreshed[0]['tracks']] == ['Soul'] * 6
    assert store.recommendations.head('home') == head
    assert [track['genre'] for track in tracks] == ['Soul'] * 6 + ['Blues'] * 6
