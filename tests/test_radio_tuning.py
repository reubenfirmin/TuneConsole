"""Shared controls, bounded NOW evidence and staggered radio alternatives."""
from types import SimpleNamespace
from tests.enrichment_support import seed_enrichment

import numpy as np
import pytest

from tests.conftest import store as memory_store
from yt_playlist.rec import embedding_vectors as embed, radio_tuning, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates, CLUSTER_POOL_LIMIT
from yt_playlist.rec.radio_directions import RadioDirections
from tests.musical_support import prepare_profiles
from yt_playlist.core import artists, catalog, radio_provenance
from yt_playlist.core.db_writes import atomic_write
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.prepared_fixture_support import prepared_template, roots
from tests.store_fixture_support import file_consumer_store as store


def prepare_tuning(store):
    modes, bundles, tracks = [], {}, []
    for mid, (genre, label) in enumerate((("ambient", "Ambient"), ("techno", "Techno")), 1):
        vector = np.eye(2, dtype=np.float32)[mid - 1]
        modes.append(dict(mode_id=mid, label=genre, families=[[genre, 1]], centroid=vector,
                          size=20, rep_keys=[]))
        familiar, fresh = [], []
        for i in range(12):
            vid = f"m{mid}-{i}"
            key = f'{vid}|artist {vid}'
            track = dict(key=key, video_id=vid, title=vid, artist=f"Artist {vid}", genre=label, year="2001")
            (fresh if i >= 6 else familiar).append(track)
            tracks.append(track)
        bundles[str(mid)] = dict(wheelhouse=familiar, fresh=fresh)
    seed_publication_subjects(store, [{**track, 'duration_s': 200, 'year': '2001'} for track in tracks])
    with atomic_write(store):
        for tid, track in enumerate(tracks, 1):
            catalog.choose_field(store.conn, tid, 'energy', .02 if track['genre'] == 'Ambient' else .98)
    store.modes.replace_modes(modes, retired_ids=[], now=1)
    publish_bundles(store, bundles)
    prepare_profiles(store)
    return tracks


@pytest.fixture(scope='module')
def radio_tuning_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, prepare_tuning) as template:
        yield template


@pytest.fixture
def prepared(store, radio_tuning_template):
    return radio_tuning_template.restore(store)


def test_prepared_tuning_copies_keep_owned_files_and_mutations_private(
        memory_store, file_backed_store, radio_tuning_template):
    store = memory_store
    template = radio_tuning_template
    first, second = template.restore(store), template.restore(file_backed_store)
    assert len(first) == 24 and first == second == template.result
    assert len(template.source.modes.list_modes(active_only=True)) == 2
    keys = [track['key'] for track in first]
    profiles = template.source.musical_profiles.by_keys(keys)
    assert len(profiles) == 24
    for target in (store, file_backed_store):
        assert target.musical_profiles.by_keys(keys) == profiles
        owned = roots(target.conn)
        for name, source in template.files.items():
            assert owned[name] != source
            for path in source.rglob('*'):
                if path.is_file():
                    copy = owned[name] / path.relative_to(source)
                    assert copy.read_bytes() == path.read_bytes()
                    assert (copy.stat().st_dev, copy.stat().st_ino) != (path.stat().st_dev, path.stat().st_ino)
    assert set(roots(store.conn).values()).isdisjoint(roots(file_backed_store.conn).values())
    first[0]['title'] = 'Private track edit'
    store._fixture_bundle_payload['1']['wheelhouse'].clear()
    store.settings.set_setting('private_tuning_fixture', 'first')
    assert second == template.result
    for other in (template.source, file_backed_store):
        assert other.settings.get_setting('private_tuning_fixture') is None
        assert len(other._fixture_bundle_payload['1']['wheelhouse']) == 6


def candidate(store, **values):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, **values})
    result.rng.seed(3)
    return result


@pytest.mark.parametrize("bad", [None, [], {}, {**radio_tuning.DEFAULTS, "other": .5},
    {**radio_tuning.DEFAULTS, "energy": float("nan")}, {**radio_tuning.DEFAULTS, "energy": float("inf")},
    {**radio_tuning.DEFAULTS, "comfort": True}, {**radio_tuning.DEFAULTS, "theme": -.1}])
def test_controls_reject_invalid_input(bad):
    with pytest.raises(ValueError):
        radio_tuning.controls(bad)


def test_discovery_and_comfort_share_prepared_fresh_source(store, prepared):
    def sequence(comfort):
        c = candidate(store, comfort=comfort)
        history = [prepared[0]]
        for _ in range(5):
            history.append(c.next_track(history))
        return sum(bool(t.get("_radio_fresh")) for t in history[1:])
    assert sequence(0) >= 4
    assert sequence(1) == 0


def test_middle_uses_style_and_energy_while_themed_pins_the_playing_genre(store, prepared):
    chill = candidate(store, theme=.5, energy=0)
    chill.now_posterior, chill.now_gain = {"2": 1}, 2
    for _ in range(5):
        assert chill.next_track([prepared[0]], target=prepared[-1])["genre"] == "Ambient"
    full = candidate(store, theme=.5, energy=1)
    assert full.next_track([prepared[0]], target=prepared[-1])["genre"] == "Techno"
    themed = candidate(store, theme=1)
    themed.now_posterior, themed.now_gain = {"2": 1}, 2
    assert themed.next_track([prepared[0]], target=prepared[-1])["genre"] == "Ambient"


def test_enrichment_updates_visible_metadata_without_rejecting_a_weak_genre_prior(store, prepared):
    c = candidate(store, energy=0)
    tid = store.tracks.upsert_track("missing", "Missing", "Artist", "", 200)
    track = {"key": store.tracks.catalog_subject(tid)["identity_key"], "video_id": "missing"}
    c.retain([track])
    seed_enrichment(store, tid, "hardcore", "2020")
    prepare_profiles(store)
    assert c.refresh_metadata([track])
    assert track["year"] == "2020"
    assert c.fits(track)
    assert not c.refresh_metadata([track])

    # An explicit recording correction can reject a future track. This update
    # changes only its prepared profile, with unchanged visible metadata.
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .95)
    prepare_profiles(store)
    assert c.refresh_metadata([track])
    assert not c.fits(track)
    assert not c.refresh_metadata([track])


@pytest.fixture(params=['feedback', 'artist', 'genre', 'overlong'])
def cold_start_catalog(store, request):
    rejection = request.param
    account = store.accounts.upsert_account('main', 'cred', None, True)
    rec_params.set_param(store, 'max_track_minutes', 5)
    tracks = [dict(key=f'cold-{index}|artist {index}', video_id=f'cold-{index}',
                   title=f'cold-{index}', artist=f'Artist {index}',
                   duration_s=9000 if index < 2 * CLUSTER_POOL_LIMIT and rejection == 'overlong' else 180,
                   genre='Jazz' if index < 2 * CLUSTER_POOL_LIMIT and rejection == 'genre' else None)
              for index in range(3 * CLUSTER_POOL_LIMIT + 7)]
    # Pagination consumes completed library membership, not provider ingestion.
    # Keep all three full pages and the short tail in actual SQL.
    seed_publication_subjects(store, tracks)
    if rejection == 'feedback':
        with store.conn:
            store.conn.executemany(
                'INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at) VALUES (?,?,?,\'\',1000)',
                [('sync', track['key'], 'dislike')
                 for track in tracks[:2 * CLUSTER_POOL_LIMIT]])
    elif rejection == 'artist':
        with atomic_write(store):
            for track in tracks[:2 * CLUSTER_POOL_LIMIT]:
                artist_id = artists.state_id(store.conn, track['artist'])
                store.conn.execute(
                    'INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at,artist_id,target_type) '
                    "VALUES ('for_you',?,'mute','',1000,?,'artist')",
                    ('artist:' + track['artist'], artist_id))
    elif rejection == 'genre':
        family = store.genres.feature_axes(['Jazz'])['Jazz'][0]
        store.rec_model.set_weight('genre:' + family, 0, lo=0, now=1000)
    assert store.settings.get_setting('rec_content_model') is None
    expected = {track['video_id'] for track in tracks[2 * CLUSTER_POOL_LIMIT:]}
    return SimpleNamespace(account=account, expected=expected, tracks=tracks, rejection=rejection)


def test_cold_start_pages_past_ineligible_prefix_without_overfilling(store, monkeypatch, cold_start_catalog):
    pages, read = [], store.radio_catalog.fallback_tracks

    def paged(limit, *, after=0):
        rows = read(limit, after=after)
        pages.append((after, [row['id'] for row in rows]))
        assert limit == CLUSTER_POOL_LIMIT and len(rows) <= limit
        return rows

    monkeypatch.setattr(store.radio_catalog, 'fallback_tracks', paged)
    c = candidate(store)
    assert len(c.pools['local']) == CLUSTER_POOL_LIMIT
    assert {t['video_id'] for t in c.pools['local']} <= cold_start_catalog.expected
    assert len({t['key'] for t in c.pools['local']}) == CLUSTER_POOL_LIMIT
    assert len(pages) == 3 and all(len(ids) == CLUSTER_POOL_LIMIT for _, ids in pages)
    assert [cursor for cursor, _ in pages] == [0, pages[0][1][-1], pages[1][1][-1]]
    c.close()


def test_cold_start_routes_offer_eligible_tracks_and_launch_without_prepared_output(
        store, monkeypatch, cold_start_catalog):
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient, _track
    from yt_playlist.web.app import create_app

    expected, account = cold_start_catalog.expected, cold_start_catalog.account
    # Exercise real seed/start routes without prepared output or a supplied pool.
    provider = FakeClient(catalog={v: _track(v, v, f'Artist {v.split("-")[-1]}') for v in expected})
    app = create_app(store, lambda: {account: provider}, now_fn=lambda: 100_000)
    ctx, frames = app.state.ctx, []
    ctx.bridge._send = lambda frame: None
    monkeypatch.setattr(ctx.bridge, 'send_control', lambda frame, **kw: frames.append(frame) or True)
    monkeypatch.setattr(ctx.radio.work, 'start_thread', lambda *args, **kw: None)
    store.settings.set_setting('onboard_listening_format', 'radio')
    try:
        with TestClient(app, base_url='http://127.0.0.1') as client:
            offer = client.get('/radio/seeds').json()
            assert len(offer['tracks']) == 16
            assert {t['video_id'] for t in offer['tracks']} <= expected
            if cold_start_catalog.rejection == 'overlong':
                started = client.post('/radio/start', json={
                    'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id'],
                    'launch_id': 'cold-prefix'}).json()
                assert started['ok']
                assert len(provider.created) == len(provider.added) == len(frames) == 1
                assert set(provider.added[0][1]) <= expected
                assert client.post('/radio/stop', json={'launch_id': 'cold-prefix'}).json()['ok']
    finally:
        ctx.radio.suspend()
        ctx.radio.work.stop_executor()


def test_cold_start_exhausts_the_short_tail_without_choices(store, monkeypatch, cold_start_catalog):
    # Exhaustion walks every remaining page, then terminates with no choices.
    with store.conn:
        store.conn.executemany(
            "INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at) VALUES ('sync',?,'dislike','',1000)",
            [(track['key'],) for track in cold_start_catalog.tracks[2 * CLUSTER_POOL_LIMIT:]])
    pages, read = [], store.radio_catalog.fallback_tracks

    def paged(limit, *, after=0):
        rows = read(limit, after=after)
        pages.append(rows)
        assert limit == CLUSTER_POOL_LIMIT and len(rows) <= limit
        return rows

    monkeypatch.setattr(store.radio_catalog, 'fallback_tracks', paged)
    c = candidate(store)
    assert not c.pools['local'] and not c.seed_offer()
    assert len(pages) == 4 and len(pages[-1]) == 7
    c.close()


def test_cold_start_cancels_after_one_rejected_page(store, monkeypatch, cold_start_catalog):
    from yt_playlist.core.activity import WorkClosing, cancellable_work

    # Cancel after one rejected page, before admitting another page.
    pages, read = [], store.radio_catalog.fallback_tracks

    def paged(limit, *, after=0):
        rows = read(limit, after=after)
        pages.append(rows)
        return rows

    monkeypatch.setattr(store.radio_catalog, 'fallback_tracks', paged)
    with pytest.raises(WorkClosing), cancellable_work(lambda: bool(pages)):
        candidate(store)
    assert len(pages) == 1


def test_cold_start_combines_library_and_discovery_with_bounded_lookahead(store):
    familiar = [dict(key=f'song {i}|artist {i}', video_id=f'v{i}', title=f'Song {i}',
                     artist=f'Artist {i}', duration_s=200) for i in range(CLUSTER_POOL_LIMIT + 1)]
    fresh = [dict(key=f'fresh{i}', video_id=f'f{i}', title=f'Fresh {i}', artist=f'New Artist {i}',
                  duration_s=200) for i in range(CLUSTER_POOL_LIMIT + 1)]
    seed_publication_subjects(store, familiar + fresh, discoveries=[track['key'] for track in fresh])
    publish_bundles(store, {}, fresh=[{key: value for key, value in track.items() if key != 'duration_s'}
                                     for track in fresh])
    c = candidate(store, comfort=0)
    assert len(c.pools["local"]) == CLUSTER_POOL_LIMIT
    assert any(not t.get("_radio_fresh") for t in c.pools["local"])
    assert any(t.get("_radio_fresh") for t in c.pools["local"])
    assert {t['video_id'] for t in c.pools['local'] if t.get('_radio_fresh')} <= {t['video_id'] for t in fresh}


@pytest.mark.parametrize("cache", ["fresh", "expired", "different_model"])
def test_retuning_reuses_now_without_loading_library_vectors(store, prepared, monkeypatch, cache):
    previous = candidate(store)
    previous.now_posterior, previous.now_gain = {"1": 1., "retired": .5}, 2.
    if cache == "different_model":
        previous.encoding_model = "old-model"
    now = 100_000 + (radio_tuning.NOW_REFRESH_SECONDS if cache == "expired" else 1)

    def unexpected_load(*args, **kwargs):
        pytest.fail("Retuning must use cached NOW evidence, without a full library snapshot or matrix")

    monkeypatch.setattr(store.rec_model, "content_generation", unexpected_load)
    monkeypatch.setattr(embed, "load_content_vectors", unexpected_load)
    updated = RadioCandidates(store, now, {**radio_tuning.DEFAULTS, "comfort": 0}, now_source=previous)
    if cache == "fresh":
        assert updated.now_posterior == {"1": 1.}
        assert updated.now_gain == 2.
        assert updated.now_sampled_at == previous.now_sampled_at
        assert updated.now_posterior is not previous.now_posterior
    else:
        assert updated.now_posterior == {}
        assert updated.now_gain == 0
        assert now - updated.now_sampled_at >= radio_tuning.NOW_REFRESH_SECONDS
    assert updated.tuning["comfort"] == 0
    assert updated.next_track([prepared[0]])


@pytest.mark.parametrize("base", [-2., 0., 2.])
def test_now_bonus_is_positive_bounded_and_neutral_at_zero_gain(base):
    assert radio_tuning.now_bonus(base, 1, 0) == base
    assert radio_tuning.now_bonus(base, .5, 2) > base
    assert radio_tuning.now_bonus(base, 1, 2) == pytest.approx(base + .12)


@pytest.mark.parametrize("evidence", ["organic", "thin", "old", "radio", "stale_dimensions", "gain_zero"])
def test_now_uses_persisted_confidence_gated_evidence_without_sampling_on_picks(store, prepared, monkeypatch, evidence):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    keys = [t["key"] for t in prepared[:3]]
    vectors = np.array([[1., 0.]] * 3, dtype=np.float32)
    if evidence == "stale_dimensions":
        vectors = np.array([[1., 0., 0.]] * 3, dtype=np.float32)
    reads = []
    def load(*args, **kwargs):
        reads.append(1)
        return keys, vectors, {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", load)
    if evidence == "gain_zero":
        rec_params.set_param(store, "now_gain", 0)
    with store.conn:
        radio_provenance.remember(store.conn, 'PLRADIO', iid)
    assert radio_provenance.ids(store.conn) == ['PLRADIO']
    for i, key in enumerate(keys[:2] if evidence == "thin" else keys):
        at = 100_000 - i * 60 - (7 * 3600 if evidence == "old" else 0)
        store.history.record_play_event(iid, key, prepared[i]["video_id"], at,
            playlist_ytm_id="PLRADIO" if evidence == "radio" else None)
    c = candidate(store)
    assert c.now_posterior == ({"1": 1.} if evidence == "organic" else {})
    before = len(reads)
    for _ in range(5):
        c.next_track([prepared[0]])
    assert len(reads) == before
    if evidence == "gain_zero":
        assert not reads


def test_persisted_now_evidence_changes_a_future_choice_and_keeps_existing_directions(store, prepared, monkeypatch):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    neutral = candidate(store, theme=0)
    before = neutral.next_track([])
    preferred = [t for t in prepared if t["genre"] != before["genre"]][:3]
    keys = [t["key"] for t in preferred]
    vector = [1., 0.] if preferred[0]["genre"] == "Ambient" else [0., 1.]
    monkeypatch.setattr(embed, "load_content_vectors", lambda *_args, **_kwargs: (
        keys, np.array([vector] * len(keys), dtype=np.float32), {k: i for i, k in enumerate(keys)}))
    for i, track in enumerate(preferred):
        store.history.record_play_event(iid, track["key"], track["video_id"], 100_000 - i * 60)
    influenced = candidate(store, theme=0)
    assert influenced.next_track([])["genre"] == preferred[0]["genre"]
    assert influenced.next_track([])["genre"] != before["genre"]

    plans = RadioDirections(influenced, prepared[0])
    history = [prepared[0]]
    plans.update(history, [], 0)
    selected = plans.slots[2]
    plans.activate(selected["id"])
    plans.update(history, [], 0)
    saved = plans.public([])
    # Refreshing evidence only affects future planning, never a selected or visible plan.
    rec_params.set_param(store, "now_gain", 0)
    influenced.refresh_now(100_001)
    assert not plans.update(history, [], 0)
    assert plans.public([]) == saved
    assert plans.active == 2
    assert plans.slots[2]["id"] == selected["id"]


def directions():
    tracks = [dict(key=str(i), video_id=str(i), artist=str(i)) for i in range(1000)]
    def pick(history, excluded=(), **kwargs):
        used = set(excluded) | {t["video_id"] for t in history}
        return next((t for t in tracks if t["video_id"] not in used), None)
    pool = SimpleNamespace(next_track=pick, direction_anchor=lambda h, a, **kw: pick(h + a, **kw),
                           cluster_for=lambda t: "local", modes={})
    return RadioDirections(pool, tracks[0]), [tracks[0]]


def test_decay_replaces_one_unused_stream_per_song_and_polls_are_stable():
    d, history = directions()
    d.update(history, [], 0)
    assert list(d.decay.values()) == [5, 4, 3]
    assert [(b["decay_remaining"], b["decay_total"]) for b in d.public([]) if not b["active"]] == [
        (5, 5), (4, 4), (3, 3)]
    for transition in range(1, 10):
        previous = [b["id"] for b in d.slots]
        history.append(d.slots[d.active]["tracks"][0])
        d.update(history, [], transition)
        assert sum(d.slots[i]["id"] != previous[i] for i in range(4) if i != d.active) == int(transition >= 3)
        if transition < 3:
            assert [(b["decay_remaining"], b["decay_total"]) for b in d.public([]) if not b["active"]] == [
                (5 - transition, 5), (4 - transition, 4), (3 - transition, 3)]
        else:
            assert sorted(d.decay.values()) == list(range(transition + 1, transition + 4))
        stable = [b["id"] for b in d.slots]
        assert not d.update(history, [], transition)
        assert [b["id"] for b in d.slots] == stable


def test_switch_preserves_other_deadlines_and_force_staggers_five_four_three():
    d, history = directions()
    d.update(history, [], 0)
    saved = dict(d.decay)
    d.activate(d.slots[1]["id"])
    d.update(history, [], 0)
    assert 1 not in d.decay
    assert all(d.decay[slot] == saved[slot] for slot in (2, 3))
    assert d.decay[0] == saved[1]
    d.update(history, [], 7, force=True)
    assert list(d.decay.values()) == [12, 11, 10]
    assert [b["decay_remaining"] for b in d.public([]) if not b["active"]] == [5, 4, 3]
