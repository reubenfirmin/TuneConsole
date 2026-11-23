"""Shared controls, bounded NOW evidence and staggered radio alternatives."""
from types import SimpleNamespace
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from tests.enrichment_support import seed_enrichment

import numpy as np
import pytest

from tests.conftest import store as memory_store
from tests.group_support import publish_keyed
from yt_playlist.rec import embedding_vectors as embed, musical_character, radio_tuning, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates, CLUSTER_POOL_LIMIT
from yt_playlist.rec.radio_directions import RadioDirections
from tests.musical_support import prepare_profiles, publish_intensities
from yt_playlist.core import artists, radio_provenance
from yt_playlist.core.db_writes import atomic_write
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.prepared_fixture_support import prepared_template, roots
from tests.store_fixture_support import file_consumer_store as store


def prepare_tuning(store):
    tracks = []
    genres = ("Ambient", "Techno")
    for mid, label in enumerate(genres, 1):
        for i in range(12):
            vid = f"m{mid}-{i}"
            key = f'{vid}|artist {vid}'
            track = dict(key=key, video_id=vid, title=vid, artist=f"Artist {vid}", genre=label, year="2001")
            tracks.append(track)
    seed_publication_subjects(store, [{**track, 'duration_s': 200, 'year': '2001'} for track in tracks],
                              discoveries=[track['key'] for track in tracks if int(track['video_id'].split('-')[1]) >= 6])
    store.rec_ledger.record_recommendation_outcomes('heard', 'start',
        [track['key'] for track in tracks if int(track['video_id'].split('-')[1]) < 6], 1)
    membership = publish_keyed(store, [(genre, [track['key'] for track in tracks if track['genre'] == genre], character)
                                       for genre, character in zip(genres, (.1, .9))])
    bundles = {}
    for track in tracks:
        version = membership[track['key']]
        lane = 'fresh' if int(track['video_id'].split('-')[1]) >= 6 else 'wheelhouse'
        bundles.setdefault(version, {}).setdefault(lane, []).append(track)
    publish_bundles(store, bundles)
    prepare_profiles(store)
    publish_intensities(store, {track['key']: .02 if track['genre'] == 'Ambient' else .98 for track in tracks})
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
    groups = template.source.taste_groups.leaf_members(template.source.taste_groups.head_generation())
    assert len(set(groups.values())) == 2
    first_group = groups[first[0]['key']]
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
    store._fixture_bundle_payload[first_group]['wheelhouse'].clear()
    store.settings.set_setting('private_tuning_fixture', 'first')
    assert second == template.result
    for other in (template.source, file_backed_store):
        assert other.settings.get_setting('private_tuning_fixture') is None
        assert len(other._fixture_bundle_payload[first_group]['wheelhouse']) == 6


def candidate(store, **values):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, **values})
    result.rng.seed(3)
    return result


@pytest.mark.parametrize("bad", [None, [], {}, {**radio_tuning.DEFAULTS, "other": .5},
    {**radio_tuning.DEFAULTS, "energy": float("nan")}, {**radio_tuning.DEFAULTS, "energy": float("inf")},
    {**radio_tuning.DEFAULTS, "comfort": True}, {**radio_tuning.DEFAULTS, "theme": -.1},
    {**radio_tuning.DEFAULTS, "journey": "road_trip"}, {**radio_tuning.DEFAULTS, "journey": []}])
def test_controls_reject_invalid_input(bad):
    with pytest.raises(ValueError):
        radio_tuning.controls(bad)


def test_stream_sequencing_changes_the_saved_future_and_survives_rejoin(store, prepared, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.radio_session_support import play, radio_controller
    from yt_playlist.library import generated_acquisition
    from yt_playlist.web import radio_session
    from yt_playlist.web.routes import radio_session as routes

    years = {track['video_id']: 1960 + 10 * (index % 6) for index, track in enumerate(prepared)}
    with atomic_write(store):
        store.conn.executemany('UPDATE track_sources SET mb_year=? WHERE video_id=?',
                               [(year, video) for video, year in years.items()])
    controls = {**radio_tuning.DEFAULTS, 'theme': 1., 'comfort': 1.}
    create = generated_acquisition.create_generated_playlist
    with radio_controller(monkeypatch, store, prepared) as (controller, remote, frames, _, _):
        monkeypatch.setattr(radio_session, 'RadioCandidates', RadioCandidates)
        monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create)
        remote.get_song = lambda video: {'videoDetails': {'lengthSeconds': '200'}}
        controller.ctx.now_fn = lambda: 1_000_000.
        current = prepared[0]['video_id']
        assert controller.start(current)['ok']
        assert len(controller.session.tracks) == 1
        play(controller, current)
        assert len(controller.session.tracks) == 3
        session = controller.session
        history = controller.status()['history']
        assert history
        app = FastAPI()
        app.include_router(routes.build(controller.ctx))
        with TestClient(app) as client:
            upcoming = []
            for journey in ('time_machine', 'throwback'):
                response = client.post('/radio/tune', json={**controls, 'journey': journey}).json()
                assert response['ok'] and response['tuning']['journey'] == journey
                controller.advance(session)
                upcoming.append(remote.ids[1])
                assert controller.session is session
                assert remote.ids[0] == current and controller.status()['history'] == history
                assert controller.ctx.bridge.now_playing['video_id'] == current
                assert len(frames) == 1
            assert years[upcoming[0]] < years[upcoming[1]]
            assert controller.persistence.load()['tuning']['journey'] == 'throwback'
            energy_start = session.directions.energy_start
            assert energy_start == len(session.heard)
        controller.suspend()
        rejoined = radio_session.PlaylistRadio(controller.ctx)
        monkeypatch.setattr(rejoined.work, 'start_thread', lambda *args, **kwargs: None)
        try:
            rejoined.join()
            assert rejoined.session
            assert rejoined.status()['tuning']['journey'] == 'throwback'
            assert rejoined.session.directions.energy_start == energy_start
            restored = rejoined.tune({**controls, 'journey': None})
            assert restored['ok'] and restored['tuning'] == controls
            assert rejoined.status()['history'] == history
        finally:
            rejoined.suspend()
            rejoined.work.stop_executor()


@pytest.mark.parametrize('handoff', ['queued', 'uncertain', 'restored', 'restored_play_before_fill',
                                     'settled_before_admission', 'settled_during_admission',
                                     'settled_between_applies', 'settled_after_first_apply', 'settled_then_restored',
                                     'playing_during_admission', 'playing_during_first_apply'])
def test_style_change_resequences_an_explicit_song_continuation(store, prepared, monkeypatch, handoff, request):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.radio_session_support import play, radio_controller
    from yt_playlist.library import generated_acquisition
    from yt_playlist.web import radio_session
    from yt_playlist.web.routes import radio_session as routes

    years = {track['video_id']: 1960 + 10 * (index % 6) for index, track in enumerate(prepared)}
    with atomic_write(store):
        store.conn.executemany('UPDATE track_sources SET mb_year=? WHERE video_id=?',
                               [(year, video) for video, year in years.items()])
    controls = {**radio_tuning.DEFAULTS, 'theme': 1., 'comfort': 1.}
    playing = handoff.startswith('playing_')
    recovered = handoff.startswith('restored')
    settled = handoff.startswith('settled_') or playing
    repeated = handoff in ('settled_between_applies', 'settled_after_first_apply', 'playing_during_first_apply')
    create = generated_acquisition.create_generated_playlist
    with radio_controller(monkeypatch, store, prepared) as (controller, remote, frames, _, _):
        monkeypatch.setattr(radio_session, 'RadioCandidates', RadioCandidates)
        monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create)
        remote.get_song = lambda video: {'videoDetails': {'lengthSeconds': '200'}}
        controller.ctx.now_fn = lambda: 1_000_000.
        current = prepared[0]['video_id']
        assert controller.start(current)['ok']
        play(controller, current)
        session = controller.session
        history = controller.status()['history']

        def restart(previous):
            previous.suspend()
            reopened = type(previous)(previous.ctx)
            previous.ctx.radio = reopened
            monkeypatch.setattr(reopened, '_start_monitor', lambda session: None)
            request.addfinalizer(reopened.work.stop_executor)
            request.addfinalizer(reopened.suspend)
            state = reopened.join()
            assert state['active'] and reopened.session
            assert reopened.session.episode_id == previous.session.episode_id
            reopened.listening.observe(reopened.session)
            return reopened, reopened.session

        def displayed_history(rows):
            return [(row['video_id'], row['title'], row['artist']) for row in rows]

        app = FastAPI()
        app.include_router(routes.build(controller.ctx))
        with TestClient(app) as client:
            assert client.post('/radio/tune', json={**controls, 'journey': 'throwback'}).json()['ok']
            controller.advance(session)
            controller.listening.fill_tuned_streams(session, cancelled=lambda: False)
            state = controller.status()
            branch = next(direction for direction in state['directions'] if direction['active'])
            old_plan = [track['video_id'] for track in branch['tracks']]
            assert len(old_plan) == 6
            skipped, target, old_next = old_plan[:3]
            choice = {'proposal_id': branch['id'], 'menu_id': state['menu_id'], 'video_id': target}
            assert client.post('/radio/select', json=choice).json()['ok']

            if handoff == 'uncertain' or recovered:
                remote.lose_response = True
                remote.after_add = lambda: setattr(remote, 'fail_read', True)
                with pytest.raises(RuntimeError, match='Read response lost'):
                    controller.advance(session)
                assert session.replacement.waiting and target in remote.ids
                remote.lose_response = False
                remote.after_add = lambda: None

            queued = list(remote.ids)

            def settle_song():
                with ThreadPoolExecutor(max_workers=1) as worker:
                    worker.submit(controller.advance, session).result(timeout=5)
                    if playing:
                        worker.submit(play, controller, target).result(timeout=5)
                assert session.requested_direction is session.replacement is None
                assert remote.ids[:2] == [current, target] and len(frames) == 2
                if playing:
                    assert remote.ids[2] == old_next

            with monkeypatch.context() as admission:
                if handoff in ('settled_before_admission', 'settled_then_restored'):
                    settle_song()
                    if handoff == 'settled_then_restored':
                        controller, session = restart(controller)
                elif settled:
                    reserve = controller.reserve_tuning

                    def reserve_then_settle():
                        assert session.requested_direction['play']['video_id'] == target
                        serial = reserve()
                        settle_song()
                        assert controller.tuning_serial == serial
                        return serial

                    admission.setattr(controller, 'reserve_tuning', reserve_then_settle)
                first_style = 'warm_up' if repeated else 'time_machine'
                response = client.post('/radio/tune', json={**controls, 'journey': first_style}).json()
                assert response['ok'] and response['tuning']['journey'] == first_style
            if repeated:
                if not playing:
                    assert session.requested_direction['tracks'][0]['video_id'] == target
                if handoff == 'settled_after_first_apply':
                    controller.advance(session)
                    assert session.requested_direction is session.replacement is None
                    assert remote.ids[:2] == [current, target] and len(frames) == 2
                response = client.post('/radio/tune', json={**controls, 'journey': 'time_machine'}).json()
                assert skipped in session.requested_direction['excluded']
            assert response['ok'] and response['tuning']['journey'] == 'time_machine'
            if settled:
                assert len(frames) == 2 and remote.ids[:2] == [current, target]
            else:
                assert len(frames) == 1 and remote.ids == queued
            expected_history = [current, target] if playing else [current]
            assert displayed_history(controller.status()['history'][:len(history)]) == displayed_history(history)
            assert [track['video_id'] for track in controller.status()['history']] == expected_history
            saved = controller.persistence.load()
            assert saved['tuning']['journey'] == 'time_machine'
            if not settled:
                pending = saved['requested_direction'] if handoff == 'queued' else saved['replacement']
                assert pending['resequence_after'] == target and 'play' not in pending

        if recovered:
            controller, session = restart(controller)

        controller.advance(session)
        assert session.replacement is None
        if handoff == 'restored_play_before_fill':
            play(controller, target)
            playing = True
            expected_history = [current, target]
        controller.listening.fill_tuned_streams(session, cancelled=lambda: False)
        assert controller.session is session
        assert remote.ids[:2] == [current, target]
        if recovered:
            assert len(frames) == 1
        else:
            assert len(frames) == 2 and frames[-1]['url'].endswith(f'v={target}&list=PLSESSION')
        assert remote.adds.count([target]) == 1
        assert [track['video_id'] for track in controller.status()['history']] == expected_history
        active = next(direction for direction in controller.status()['directions'] if direction['active'])
        offered = [track['video_id'] for track in active['tracks']]
        assert skipped not in offered
        if not playing:
            assert remote.ids == [current, target]
            assert offered[0] == target
            play(controller, target)
            assert remote.ids[2] == offered[1]
        assert session.edits.handoff is None
        assert len(frames) == (1 if recovered else 2)
        assert target not in {video for batch in remote.removes for video in batch}
        upcoming = remote.ids[2]
        assert upcoming != skipped
        active = next(direction for direction in controller.status()['directions'] if direction['active'])
        assert [t['video_id'] for t in active['tracks']][:1] == [upcoming]
        assert [t['video_id'] for t in controller.status()['history']] == [current, target]


def prepare_rotation(store):
    tracks = [dict(key=f'{kind}-{i}', video_id=f'{kind}-{i}', title=f'{kind}-{i}',
        artist=f'Artist {kind}-{i}', genre='Ambient', duration_s=200)
        for kind in ('familiar', 'unplayed', 'new') for i in range(128)]
    seed_publication_subjects(store, tracks, discoveries=[t['key'] for t in tracks[256:]])
    for track in tracks:
        track.pop('duration_s')
    account = store.accounts.upsert_account('rotation', None, True)
    with atomic_write(store):
        store.conn.executemany('INSERT INTO play_events(account_id,recording_id,video_id,played_at,is_generated) '
            'SELECT ?,r.id,?, ?,0 FROM recordings r WHERE r.identity_key=?',
            [(account, track['video_id'], 1 + day * 86400, track['key'])
             for track in tracks[:128] for day in range(10)])
        store.conn.executemany("INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at) "
            "VALUES ('like',?,'like','',1)", [(track['key'],) for track in tracks[:128]])
    keys = [track['key'] for track in tracks]
    membership = publish_keyed(store, [('Ambient', keys, .1)])
    version, = set(membership.values())
    publish_bundles(store, {version: {'wheelhouse': tracks[:48], 'comfort': tracks[:48],
        'explore': tracks[128:224], 'fresh': tracks[256:]},
        '_radio': {'space': embed.content_space_id(store), 'members': {version: keys}}},
        fresh=tracks[256:])
    return tracks


@pytest.fixture(scope='module')
def radio_rotation_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, prepare_rotation) as template:
        yield template


@pytest.mark.parametrize('comfort,launch', [(0, None), (.5, None), (1, None),
    (.5, 'direct'), (.5, 'playlist'), (.5, 'legacy-checkpoint')])
def test_discovery_and_comfort_use_current_listening_and_ownership(
        store, radio_rotation_template, comfort, launch, record_property, monkeypatch):
    prepared = radio_rotation_template.restore(store)
    if launch:
        from tests.radio_session_support import radio_controller, play
        from yt_playlist.library import generated_acquisition
        from yt_playlist.web import radio_session
        from yt_playlist.web.radio_session import PlaylistRadio

        create = generated_acquisition.create_generated_playlist
        with radio_controller(monkeypatch, store, prepared) as (c, remote, _, _, _):
            # Only provider I/O is controlled. Keep real initial creation, SQL
            # readback, listening evidence, planning and checkpoint restoration.
            monkeypatch.setattr(radio_session, 'RadioCandidates', RadioCandidates)
            monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create)
            remote.get_song = lambda video: {'videoDetails': {'lengthSeconds': '200'}}
            c.ctx.now_fn = lambda: 1_000_000.
            if launch == 'playlist':
                account = store.accounts.get_master_account().id
                pid = store.playlists.upsert_playlist(account, 'PLSOURCE', 'Source', 3, '', 1)
                videos = ['unplayed-0', 'new-0', 'familiar-0']
                ids = store.tracks.track_ids_for_videos(videos)
                store.playlists.set_playlist_tracks(pid, [ids[video] for video in videos])
                result = c.start(playlist_id=pid)
            else:
                result = c.start(seed_video_id='unplayed-0')
            assert result['ok'], result
            if launch != 'playlist':
                play(c, 'unplayed-0')
            initial = list(c.session.tracks)
            assert len(initial) == 3
            assert {t['_radio_rotation_slot'] for t in initial} == {'familiar', 'unplayed', 'new'}

            def listen(video):
                key = store.tracks.identity_key_for_video(video)
                store.rec_ledger.record_recommendation_outcomes(f'launch:{video}', 'start', [key],
                    c.ctx.now_fn(), generated=True)
                play(c, video)

            for track in initial:
                assert track['_radio_rotation_slot'] == track['key'].split('-')[0]
                listen(track['video_id'])
            while len(c.session.heard) < 7:
                listen(remote.ids[-1])
            assert len(c.session.heard) == 7
            saved = c.persistence.load()
            assert all(t.get('_radio_rotation_slot') for t in saved['tracks'])
            c.suspend()
            restored = PlaylistRadio(c.ctx)
            monkeypatch.setattr(restored.work, 'start_thread', lambda *args, **kwargs: None)
            if launch == 'legacy-checkpoint':
                # Reproduce the native checkpoint: initial allocations missing,
                # and current facts now say familiar after generated starts.
                for tracks in (saved['tracks'], saved['heard']):
                    for track in tracks[:3]:
                        track.pop('_radio_rotation_slot', None)
                        track['_radio_rotation'] = 'familiar'
                monkeypatch.setattr(restored.persistence, 'load', lambda: saved)
            try:
                restored.join()
                assert restored.session
                assert restored.tune({**radio_tuning.DEFAULTS, 'theme': .51})['ok']
                directions = restored.session.directions
                assert directions.filling
                assert len(directions.slots) == 4
                first_choices = []
                for branch in directions.slots:
                    track, = branch['tracks']
                    assert track['key'].startswith('new-')
                    first_choices.append(track['key'])
                restored.listening.fill_tuned_streams(restored.session, cancelled=lambda: False)
                assert not restored.session.directions.filling
                assert [branch['tracks'][0]['key'] for branch in restored.session.directions.slots] == first_choices
                for branch in restored.session.directions.slots:
                    counts = Counter(t['key'].split('-')[0] for t in branch['tracks'])
                    assert all(1 <= counts[kind] <= 3 for kind in ('familiar', 'unplayed', 'new')), counts
                    assert counts['familiar'] == 2, counts
                # Fresh facts change after listening; allocation stays as issued.
                facts = store.radio_catalog.rotation_facts(t['key'] for t in initial)
                assert all(facts[t['key']]['heard'] for t in initial)
            finally:
                restored.suspend()
                restored.work.stop_executor()
        return

    def sequence(comfort):
        c = candidate(store, comfort=comfort)
        history = [prepared[0]]
        try:
            for _ in range(120):
                chosen = c.next_track(history)
                assert chosen and chosen['key'] not in {t['key'] for t in history}
                history.append(chosen)
            return history[1:]
        finally:
            c.close()
    chosen = sequence(comfort)
    counts = Counter(t['key'].split('-')[0] for t in chosen)
    record_property('rotation', dict(comfort=comfort, counts=dict(counts)))
    if comfort == .5:
        assert all(35 <= counts[kind] <= 45 for kind in ('familiar', 'unplayed', 'new')), counts
        for start in range(0, 120, 24):
            window = Counter(t['key'].split('-')[0] for t in chosen[start:start + 24])
            assert all(window[kind] >= 6 for kind in ('familiar', 'unplayed', 'new')), window
    elif comfort == 0:
        assert counts['new'] >= 100 and counts['unplayed'] >= 15 and not counts['familiar'], counts
    else:
        assert counts == {'familiar': 120}


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


def test_enrichment_updates_visible_metadata_and_intensity(store, prepared):
    c = candidate(store, energy=0)
    tid = store.tracks.upsert_track("missing", "Missing", "Artist", "", 200)
    track = {"key": store.tracks.catalog_subject(tid)["identity_key"], "video_id": "missing"}
    c.retain([track])
    seed_enrichment(store, tid, "hardcore", "2020")
    prepare_profiles(store)
    assert c.refresh_metadata([track])
    assert track["year"] == "2020"
    assert c.fits(track)
    assert musical_character.intensity_evidence(track) == (None, 0)
    assert not c.refresh_metadata([track])

    # A newly measured score changes the track's priority for a Chill pick,
    # while leaving the already offered track eligible as a fallback.
    publish_intensities(store, {track['key']: .95}, 1001)
    assert c.refresh_metadata([track])
    assert c.fits(track)
    assert musical_character.intensity_evidence(track) == (.95, 1)
    assert not c.refresh_metadata([track])


@pytest.fixture(params=['feedback', 'artist', 'genre', 'overlong'])
def cold_start_catalog(store, request):
    rejection = request.param
    account = store.accounts.upsert_account('main', None, True)
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
    assert store.rec_model.recording_encoder() is None
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
                seed = offer['tracks'][0]['video_id']
                started = client.post('/radio/start', json={
                    'video_id': seed, 'menu_id': offer['menu_id'],
                    'launch_id': 'cold-prefix'}).json()
                assert started['ok']
                assert len(provider.created) == len(frames) == 1
                assert not provider.added
                playlist_id = provider.created[0][0]
                assert [track['videoId'] for track in provider.get_playlist(playlist_id)['tracks']] == [seed]
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
    version = previous.cluster_for(prepared[0])
    previous.now_posterior, previous.now_gain = {version: 1., "retired": .5}, 2.
    if cache == "different_model":
        previous.encoding_model = "old-model"
    now = 100_000 + (radio_tuning.NOW_REFRESH_SECONDS if cache == "expired" else 1)

    def unexpected_load(*args, **kwargs):
        pytest.fail("Retuning must use cached NOW evidence, without a full library snapshot or matrix")

    monkeypatch.setattr(store.rec_model, "content_generation", unexpected_load)
    monkeypatch.setattr(embed, "load_content_vectors", unexpected_load)
    updated = RadioCandidates(store, now, {**radio_tuning.DEFAULTS, "comfort": 0}, now_source=previous)
    if cache == "fresh":
        assert updated.now_posterior == {version: 1.}
        assert updated.now_gain == 2.
        assert updated.now_sampled_at == previous.now_sampled_at
        assert updated.now_posterior is not previous.now_posterior
    else:
        assert updated.now_posterior == {}
        assert updated.now_gain == 0
        assert updated.now_sampled_at == now
    assert updated.tuning["comfort"] == 0
    assert updated.next_track([prepared[0]])


@pytest.mark.parametrize("base", [-2., 0., 2.])
def test_now_bonus_is_positive_bounded_and_neutral_at_zero_gain(base):
    assert radio_tuning.now_bonus(base, 1, 0) == base
    assert radio_tuning.now_bonus(base, .5, 2) > base
    assert radio_tuning.now_bonus(base, 1, 2) == pytest.approx(base + .12)


@pytest.mark.parametrize("evidence", ["organic", "thin", "old", "radio", "unmapped", "gain_zero"])
def test_now_uses_persisted_confidence_gated_evidence_without_sampling_on_picks(store, prepared, monkeypatch, evidence):
    iid = store.accounts.upsert_account("main", None, True)
    keys = [t["key"] for t in prepared[:3]]
    def load(*args, **kwargs):
        pytest.fail("Group evidence must not load library vectors")
    monkeypatch.setattr(embed, "load_content_vectors", load)
    if evidence == "gain_zero":
        rec_params.set_param(store, "now_gain", 0)
        rec_params.set_param(store, "session_alpha", 0)
    with store.conn:
        radio_provenance.remember(store.conn, 'PLRADIO', iid)
    assert radio_provenance.ids(store.conn) == ['PLRADIO']
    for i, key in enumerate(keys[:2] if evidence == "thin" else keys):
        at = 100_000 - i * 60 - (24 * 3600 if evidence == "old" else 0)
        store.history.record_play_event(iid, f'unknown-{i}' if evidence == "unmapped" else key,
            None if evidence == "unmapped" else prepared[i]["video_id"], at,
            playlist_ytm_id="PLRADIO" if evidence == "radio" else None)
    c = candidate(store)
    assert c.now_posterior == ({c.cluster_for(prepared[0]): 1.} if evidence == "organic" else {})
    monkeypatch.setattr(c, 'sample_now', load)
    for _ in range(5):
        c.next_track([prepared[0]])


def test_persisted_now_evidence_changes_a_future_choice_and_keeps_existing_directions(store, prepared, monkeypatch):
    iid = store.accounts.upsert_account("main", None, True)
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


def test_filling_a_recovered_selection_keeps_its_skipped_prefix():
    import json
    from threading import RLock

    from yt_playlist.rec.radio_policy import RadioPolicy
    from yt_playlist.web import radio_persistence
    from yt_playlist.web.radio_listening import RadioListening
    from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail
    from yt_playlist.web.radio_session import PlaylistRadio
    from yt_playlist.web.radio_state import Phase, Session

    menu, history = directions()
    candidates = menu.candidates
    candidates.tuning = radio_tuning.DEFAULTS
    candidates.fingerprint = 'test'
    candidates.retain = lambda tracks: None
    candidates.refresh_exclusions = lambda now: None
    candidates.blocked = candidates.muted = set()
    menu.update(history, [], 0)
    branch = menu.slots[menu.active]
    skipped, chosen = branch['tracks'][:2]
    branch['tracks'] = [chosen]
    menu.filling = True
    queue = [*history, chosen]
    session = Session(None, candidates, 1, 'Test', phase=Phase.REPLACING, tracks=queue, selected=queue,
                      heard=history, position=0, furthest=0, directions=menu)
    session.edits.begin(RadioPlaylistTail(queue, [chosen], menu.active, excluded=[skipped['video_id']]))

    def restore(state):
        return radio_persistence.restore(json.loads(state), None, 1, candidates)

    session = restore(radio_persistence.encode(session))
    lock, now = RLock(), lambda: 10.
    policy = RadioPolicy(None, now)
    controller = SimpleNamespace(lock=lock, policy=policy, session=session,
                                 _is_current=lambda s: True, _persist=lambda s: None)
    assert PlaylistRadio._replan_selection(controller, session, replacement=session.replacement)
    assert session.replacement is None

    # The monitor may resume filling a settled continuation from a later checkpoint.
    resumed = restore(radio_persistence.encode(session))
    listening = RadioListening(None, None, policy, lock, now)
    listening.fill_tuned_streams(resumed, cancelled=lambda: False)
    tracks = resumed.directions.slots[resumed.directions.active]['tracks']
    assert tracks[0] == chosen
    assert skipped not in tracks
    assert policy.next_track(resumed.candidate_view()) == tracks[1]


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
