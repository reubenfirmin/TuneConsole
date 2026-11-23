"""Opt-in actual-library Mix POST with real workers and a controlled player."""
import asyncio
from dataclasses import replace
from html.parser import HTMLParser
import hashlib
import json
import logging
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
import uvloop

from tests.conftest import FakeClient, _track
from tests.core.check_startup_background_actual import check
from tests.schema.actual_library import copy_readonly_library
from tests.library_image.fixtures import report_file


class OfferedMixes(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards, self.current, self.sections = [], None, 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'section':
            if self.current is not None:
                self.sections += 1
            elif 'gen-card' in classes:
                self.current = {'id': attrs['id'], 'tracks': []}
                self.sections = 1
        if self.current is None:
            return
        if 'gen-card-body' in classes:
            self.current.update(name=attrs.get('data-name', ''), recipe=attrs.get('data-recipe', ''))
        if 'gen-row' in classes:
            self.current['tracks'].append({key: attrs.get('data-' + attribute, '') for key, attribute in (
                ('key', 'key'), ('video_id', 'vid'), ('title', 'title'), ('artist', 'artist'),
                ('album', 'album'), ('thumbnail', 'thumb'))})

    def handle_endtag(self, tag):
        if tag == 'section' and self.current is not None:
            self.sections -= 1
            if not self.sections:
                self.cards.append(self.current)
                self.current = None


async def exercise_mix(client, ctx, store, bridge, report):
    from yt_playlist.library.sync import sync_identity

    class Provider(FakeClient):
        def create_playlist(self, title, description):
            time.sleep(1)  # Keep an external create in flight for concurrent probes.
            playlist = 'ISOLATED_MIX'
            self.created.append((title, description, playlist))
            self._tracks[playlist] = []
            return playlist

    provider, frames = Provider(), []
    account = store.accounts.get_master_account().id
    ctx.client_provider = lambda: {account: provider}

    async def send(frame):
        if frame.get('type') == 'navigate':
            frames.append(frame)
            playlist = parse_qs(urlparse(frame['url']).query)['list'][0]
            bridge.now_playing = {'playlist_ytm_id': playlist,
                'video_id': provider._tracks[playlist][0]['videoId'], 'paused': False}
            bridge.now_playing_seen_at = time.monotonic()
        if 'id' in frame:
            bridge.resolve(frame['id'], 503, '{}')

    bridge.connect(send, asyncio.get_running_loop())
    before = time.monotonic()
    response = await client.get('/')
    assert response.status_code == 200
    cards = [card for card in OfferedMixes(response.text).cards if card['tracks']]
    report['mix_offer'] = {'seconds': time.monotonic() - before,
                          'cards': [{'id': card['id'], 'tracks': len(card['tracks'])} for card in cards]}
    assert cards, 'The actual copied account did not render a playable prepared Mix'
    selected = cards[0]
    for track in selected['tracks']:
        provider._catalog[track['video_id']] = _track(track['video_id'], track['title'], track['artist'])
    report['controls'] += (' Mix launch uses the actual rendered card with controlled provider '
        'create/add/readback, a one-second create delay and simulated extension navigation. '
        'A real sync runs on a separate controlled account; no account unlocks or pause flags are reset.')
    sync_account = store.accounts.upsert_account('Isolated Mix sync', None, False)
    sync_client = FakeClient(playlists=[{'playlistId': 'MIX_SYNC', 'title': 'Sync check', 'count': 2}],
        tracks={'MIX_SYNC': [_track('mix-sync-a', 'Sync A', 'Fixture'), _track('mix-sync-b', 'Sync B', 'Fixture')]})
    sync = asyncio.create_task(asyncio.to_thread(sync_identity, store, sync_account, sync_client, now=time.time()))
    started = time.monotonic()
    pending = asyncio.create_task(client.post('/home/generate', data={
        'name': selected['name'], 'recipe': selected['recipe'], 'tracks': json.dumps(selected['tracks']),
        'launch_id': 'isolated-actual-mix'}))
    probes = report['mix_probes'] = []
    while not pending.done():
        for route in ('/home/generating', '/bridge/status', '/radio/status'):
            at = time.monotonic()
            probe = await asyncio.wait_for(client.get(route), 3)
            probes.append({'path': route, 'seconds': time.monotonic() - at, 'status': probe.status_code})
            assert probe.status_code == 200
            if route == '/home/generating':
                assert 'data-generation-terminal' in probe.text
        assert time.monotonic() - started < 60
        await asyncio.sleep(.1)
    response = pending.result()
    report['mix_result'] = {'seconds': time.monotonic() - started, 'status': response.status_code,
                            'body': response.text}
    assert response.status_code == 200 and 'data-generated-playlist="ISOLATED_MIX"' in response.text
    await asyncio.wait_for(sync, 15)
    deadline = time.monotonic() + 15
    while not frames and time.monotonic() < deadline:
        await asyncio.sleep(.1)
    assert len(provider.created) == len(frames) == 1
    assert 'list=ISOLATED_MIX' in frames[0]['url']
    assert [track['video_id'] for track in selected['tracks']] == [
        track['videoId'] for track in provider._tracks['ISOLATED_MIX']]
    assert bridge.now_playing['playlist_ytm_id'] == 'ISOLATED_MIX' and not bridge.now_playing['paused']
    report['mix_navigation'] = frames
    report['mix_queue'] = [track['videoId'] for track in provider._tracks['ISOLATED_MIX']]
    return 'ISOLATED_MIX'


def exercise_retained_mix(path):
    """Challenge replacement with real offers after normal workers have drained."""
    from yt_playlist.core.store import Store
    from yt_playlist.rec import home_cards, home_prepared
    import logging

    report = {'controls': 'The held board and the single replacement card come from the copied library. '
              'Only the replacement generation and assembly result are controlled.'}
    store = Store(path)
    try:
        store.init_schema()
        now = time.time()
        previous = home_prepared.peek(store, now)
        assert home_prepared.batch_complete(previous)
        complete = next(menu for menu in previous['menus'] if home_prepared._complete_menu(menu))
        proto = complete['protos'][0]
        one_card = home_cards.CardsResult(**{**complete, 'protos': [proto],
                                           'slots': [{'proto': proto, 'pool': None}]})
        originals = {surface: store.rec_proposals.get_proposals(surface)
                     for surface in (home_prepared.SURFACE, 'home_tuned')}
        original_generation = home_prepared._proposal_generation
        calls = []

        def partial(service, request):
            calls.append(len(one_card.protos))
            return replace(one_card, epoch=request.epoch)

        with patch.object(home_prepared, '_proposal_generation',
                          lambda *a, **kw: original_generation(*a, **kw) + '-partial'), \
                patch.object(home_cards.HomeCards, 'cards', partial):
            assert home_prepared._prepare(store, now) is None
        assert calls and all(count == 1 for count in calls)
        assert originals == {surface: store.rec_proposals.get_proposals(surface) for surface in originals}
        selected = home_prepared.peek(store, now + 1)
        result = home_prepared.cards(store, selected, now + 1,
            home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__)), record=False)
        assert len(result.protos) >= 4
        report.update(status='passed', selected_generation=selected['generation'],
                      retained_cards=len(result.protos), partial_attempts=calls)
    except BaseException as exc:
        report.update(status='failed', error=repr(exc))
        raise
    finally:
        report_file('/tmp/tuneconsole-649-actual-retention.json').write_text(json.dumps(report, indent=2) + '\n')
        store.conn.close()


@pytest.mark.parametrize('during_selection', [False, True])
def test_actual_library_vibe_rechecks_recording_eligibility(managed_library, monkeypatch, during_selection):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    import sqlite3

    from fastapi.testclient import TestClient

    from tests.library_image.fixtures import managed_database
    from yt_playlist.core.store import Store
    from yt_playlist.rec import home_cards, mix_tuning, ordering, taste_analysis
    from yt_playlist.web.app import create_app

    report = {'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'responses': {},
              'during_selection': during_selection}
    managed_database(report=report)
    now = report['managed_library']['created_at']
    store = Store(managed_library)
    try:
        with closing(store.conn):
            store.init_schema()
            report['recordings'] = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
            mix_tuning.reset(store)
            if not store.rec_proposals.library_taste_summary():
                taste_analysis.prepare_library_summary(store, now)
            account = store.accounts.get_master_account().id
            fake = FakeClient()
            monkeypatch.setattr('yt_playlist.core.updatecheck._raw_version', lambda: '0.0.0.dev0')
            app = create_app(store, lambda: {account: fake}, now_fn=lambda: now, background_work=False)

            def read(name, route=None):
                start = time.monotonic()
                response = (client.get(route) if route else client.post('/home/vibe', data={'vibe': 'relax'}))
                assert response.status_code == 200, response.text[:500]
                cards = {card['id']: card for card in OfferedMixes(response.text).cards if card['tracks']}
                report['responses'][name] = {'seconds': round(time.monotonic() - start, 3),
                    'tracks': {lane: [track['key'] for track in card['tracks']] for lane, card in cards.items()}}
                assert len(cards) == 4, report['responses'][name]
                assert {json.loads(card['recipe'])['vibe'] for card in cards.values()} == {'relax'}
                return cards

            with TestClient(app, base_url='http://127.0.0.1') as client:
                before = read('before')
                targets = {lane: before['gen-' + lane]['tracks'][0] for lane in ('wheelhouse', 'comfort')}
                selected, deliver = threading.Event(), threading.Event()
                original_cards = home_cards.HomeCards.cards

                def pause_after_selection(service, request):
                    result = original_cards(service, request)
                    if not selected.is_set():
                        assert result.vibe == 'relax'
                        for proto in result.protos:
                            if proto['lane'] in targets:
                                track = proto['tracks'][0]
                                targets[proto['lane']] = {key: ordering._field(track, key)
                                                         for key in ('key', 'video_id')}
                        selected.set()
                        assert deliver.wait(10)
                    return result

                def confirm_feedback():
                    disliked, recent = targets['wheelhouse'], targets['comfort']
                    response = client.post('/now-playing/rate', json={
                        'videoId': disliked['video_id'], 'action': 'dislike', 'likeStatus': 'DISLIKE'})
                    assert response.status_code == 200 and response.json()['ok'], response.text
                    assert fake.rated == [(disliked['video_id'], 'DISLIKE')]
                    assert store.tracks.item_rating(account, disliked['video_id'])['status'] == 'DISLIKE'
                    assert store.history.record_play_event(account, recent['key'], recent['video_id'], now,
                                                           source_timezone='UTC')

                if during_selection:
                    with patch.object(home_cards.HomeCards, 'cards', pause_after_selection), ThreadPoolExecutor(1) as pool:
                        pending = pool.submit(read, 'selection')
                        try:
                            assert selected.wait(10)
                            confirm_feedback()
                            assert not pending.done()
                        finally:
                            deliver.set()
                        selection = pending.result(timeout=10)
                else:
                    confirm_feedback()
                    selection = read('selection')

                disliked, recent = targets['wheelhouse'], targets['comfort']
                for name, after in (('selection', selection), ('cards', read('cards', '/home/cards')),
                                    ('feed', read('feed', '/home/feed'))):
                    assert disliked['key'] not in {track['key'] for card in after.values() for track in card['tracks']}
                    if 'gen-comfort' in after:
                        assert recent['key'] not in {track['key'] for track in after['gen-comfort']['tracks']}
                    assert report['responses'][name]['tracks'] == report['responses']['selection']['tracks']
    finally:
        phase = 'in-flight' if during_selection else 'before'
        report_file(f'/tmp/tuneconsole-1493-vibe-eligibility-{phase}.json').write_text(
            json.dumps(report, indent=2) + '\n')


def test_actual_library_mix_launch(request, tmp_path, monkeypatch):
    option = request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply a read-only source for an isolated actual-library replay')
    source = Path(option).resolve(strict=True)
    destination = tmp_path / 'tuneconsole-mix.db'
    report_path = report_file('/tmp/tuneconsole-634-actual-copy.json')
    report = {'source': str(source), 'source_access': 'SQLite mode=ro; files read only',
              'python': sys.version, 'status': 'copying', 'source_hashes': {}}
    for name in ('web/routes/home_generation.py', 'web/routes/home.py', 'web/static/playlist-generation.js',
                 'web/templates/generating.html', 'library/playlist_navigation.py'):
        path = Path(__file__).resolve().parents[2] / 'src/yt_playlist' / name
        report['source_hashes'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        destination = copy_readonly_library(source, destination, monkeypatch, report)
        report['status'] = 'running'
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        policy = asyncio.get_event_loop_policy()
        try:
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            uvloop.run(check(destination, report_file('/tmp/tuneconsole-634-actual-launch.json'),
                             launch_mix=exercise_mix, check_ownership=True))
        finally:
            asyncio.set_event_loop_policy(policy)
        exercise_retained_mix(destination)
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error=repr(exc))
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n')


def test_actual_library_ordinary_mix_replaces_an_excluded_lane(managed_library, monkeypatch):
    from contextlib import closing

    from fastapi.testclient import TestClient

    from tests.library_image.fixtures import managed_database
    from yt_playlist.core.store import Store
    from yt_playlist.rec import choice_evidence, mix_tuning, taste_analysis
    from yt_playlist.web.app import create_app

    report = {'responses': {}}
    managed_database(report=report)
    now = report['managed_library']['created_at']
    store = Store(managed_library)
    try:
        with closing(store.conn):
            store.init_schema()
            mix_tuning.reset(store)
            if not store.rec_proposals.library_taste_summary():
                taste_analysis.prepare_library_summary(store, now)
            account = store.accounts.get_master_account().id
            fake = FakeClient()
            monkeypatch.setattr('yt_playlist.core.updatecheck._raw_version', lambda: '0.0.0.dev0')
            app = create_app(store, lambda: {account: fake}, now_fn=lambda: now, background_work=False)
            cards_path = '/home/cards?prepare_attempt=1'

            def read(client, name, path):
                start = time.monotonic()
                response = client.get(path)
                assert response.status_code == 200, response.text[:500]
                cards = {card['id']: card for card in OfferedMixes(response.text).cards if card['tracks']}
                report['responses'][name] = {'seconds': round(time.monotonic() - start, 3),
                    'tracks': {lane: [track['key'] for track in card['tracks']] for lane, card in cards.items()}}
                return cards

            with TestClient(app, base_url='http://127.0.0.1') as client:
                initial = read(client, 'initial', cards_path)
                assert len(initial) == 4 and all(len(card['tracks']) == 12 for card in initial.values())
                control = read(client, 'control', '/home/feed')
                assert control.keys() == initial.keys()
                assert report['responses']['initial']['tracks'] == report['responses']['control']['tracks']
                publication = store.recommendations.head('home')
                removed = initial['gen-wheelhouse']['tracks']
                for track in removed:
                    response = client.post('/now-playing/rate', json={
                        'videoId': track['video_id'], 'action': 'dislike', 'likeStatus': 'DISLIKE'})
                    assert response.status_code == 200 and response.json()['ok'], response.text
                assert fake.rated == [(track['video_id'], 'DISLIKE') for track in removed]
                replacement = read(client, 'replacement', cards_path)
                feed = read(client, 'feed', '/home/feed')
                assert len(replacement) == len(feed) == 4
                assert all(len(card['tracks']) == 12 for card in replacement.values())
                assert report['responses']['replacement']['tracks'] == report['responses']['feed']['tracks']
                assert set(track['key'] for track in removed).isdisjoint(
                    track['key'] for card in replacement.values() for track in card['tracks'])
                assert store.recommendations.head('home') == publication

                old_offer = json.loads(initial['gen-wheelhouse']['recipe'])['opportunity_id']
                recipes = [json.loads(card['recipe']) for card in replacement.values()]
                [new_offer] = set(recipe['opportunity_id'] for recipe in recipes)
                initial_options = [json.loads(card['recipe'])['option_id'] for card in initial.values()]
                replacement_options = [recipe['option_id'] for recipe in recipes]
                # Choice evidence represents visible mix descriptions. Swapping
                # tracks behind the same descriptors keeps the same opportunity.
                assert (new_offer == old_offer) == (replacement_options == initial_options)
                opportunity = choice_evidence.opportunity(store, new_offer)
                assert [option['id'] for option in opportunity['options']] == replacement_options
    finally:
        report_file('/tmp/tuneconsole-1491-ordinary-eligibility.json').write_text(json.dumps(report, indent=2) + '\n')


def test_actual_vibe_publishes_during_worker_state_updates(managed_library, monkeypatch):
    from yt_playlist.core import activity, recording_selection
    from yt_playlist.core.store import Store
    from yt_playlist.library.vibe_composition import requests
    from yt_playlist.rec import home_cache, home_cards

    store = Store(managed_library)
    work = activity.Activity()
    entered, release = threading.Event(), threading.Event()
    builds = []
    report = {'preset': 'relax', 'builds': builds}
    try:
        store.init_schema()
        assert recording_selection.prepare(store)
        report['regular_schema'] = store.conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0]
        report['recordings'] = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
        compose = home_cards.HomeCards.cards

        def overlapping(calculation, request):
            build = {'seconds': None}
            builds.append(build)
            entered.set()
            assert release.wait(30)
            start = time.monotonic()
            result = compose(calculation, request)
            build.update(seconds=time.monotonic() - start, lanes=[card['lane'] for card in result.protos],
                         preparing=result.preparing)
            return result

        monkeypatch.setattr(home_cards.HomeCards, 'cards', overlapping)
        ctx = SimpleNamespace(store=store, work=work, now_fn=time.time, rec_worker=None,
                              logger=logging.getLogger(__name__))
        worker = requests(ctx)
        launch = 'test-vibe-overlap-launch'
        episode = 'test-vibe-overlap-episode'
        assert store.radio_checkpoints.reserve_launch(launch, episode, seeded_recovery={
            'launch_id': launch, 'episode_id': episode})
        before = time.monotonic()
        accepted, _ = worker.select(report['preset'], time.time())
        assert entered.wait(30)
        assert store.radio_checkpoints.update_seeded_dispatch(launch, 'ISOLATED_OVERLAP', started=True)
        store.evidence_files.update_maintenance(pending=True, phase='checkpoint', checkpoint_after='test-vibe-overlap')
        release.set()
        assert work.wait(30)
        report['seconds'] = time.monotonic() - before
        report['state'] = worker.state()
        assert report['state'] == {**accepted, 'phase': 'ready'}
        assert len(builds) == 1
        menu = home_cache.read(store, 'home_tuned')['menus'][0]
        assert len(menu['protos']) >= 4
        assert {card['recipe']['vibe'] for card in menu['protos']} == {report['preset']}
    finally:
        release.set()
        work.wait(30)
        report_file('/tmp/tuneconsole-1432-vibe-overlap.json').write_text(json.dumps(report, indent=2) + '\n')
        store.conn.close()
