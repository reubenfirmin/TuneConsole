"""Opt-in actual-library Mix POST with real workers and a controlled player."""
import asyncio
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
import uvloop

from tests.conftest import FakeClient, _track
from tests.core.check_startup_background_actual import check
from tests.schema.actual_library import copy_readonly_library
from tests.library_image_fixtures import report_file


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
                ('video_id', 'vid'), ('title', 'title'), ('artist', 'artist'),
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
    sync_account = store.accounts.upsert_account('Isolated Mix sync', 'isolated-mix.json', None, False)
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
    assert response.status_code == 200 and 'Saved' in response.text
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


def exercise_retained_mix(path):
    """Challenge replacement with real offers after normal workers have drained."""
    from yt_playlist.core.store import Store
    from yt_playlist.rec import home_cards, home_prepared
    import logging

    report = {'controls': 'Actual copied offers and current eligibility. Only the replacement '
              'generation token and candidate result are controlled to simulate partial preparation.'}
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        now = time.time()
        previous = home_prepared.peek(store, now)
        assert previous and home_prepared._has_complete_menu(store, previous, now)
        originals = {surface: store.rec_proposals.get_proposals(surface)
                     for surface in (home_prepared.SURFACE, 'home_tuned')}
        original_generation = home_prepared._proposal_generation
        original_cards = home_cards.HomeCards.cards
        calls = []

        def partial(service, request):
            result = original_cards(service, request)
            report['replacement_card_count_before_fault'] = len(result.protos)
            result.protos = result.protos[:1]
            lanes = {proto['lane'] for proto in result.protos}
            result.slots = [slot for slot in result.slots if slot['proto'] and slot['proto']['lane'] in lanes]
            calls.append(len(result.protos))
            return result

        with patch.object(home_prepared, '_proposal_generation',
                          lambda *a, **kw: original_generation(*a, **kw) + '-partial'), \
                patch.object(home_cards.HomeCards, 'cards', partial):
            assert home_prepared._prepare(store, now) is None
        assert calls and all(count <= 1 for count in calls)
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
