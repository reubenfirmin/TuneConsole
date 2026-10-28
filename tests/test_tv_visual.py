"""Timeline-only visual inspection, cache policy, and the playlist/account boundary."""
from types import SimpleNamespace
from io import BytesIO
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from tests.test_tv_stream import stream, video
from yt_playlist.core.identities import BridgeClient
from yt_playlist.egress import BlockedHost, EgressGuard, host_allowed
from yt_playlist.rec import tv, tv_visual
from yt_playlist.web.app import create_app


def storyboard(video_id='visual00002', interval=5000, count=50):
    return {'playerStoryboardSpecRenderer': {'spec':
        f'https://i.ytimg.com/sb/{video_id}/storyboard3_L$L/$N.jpg?sqp=preview'
        f'|160#90#{count}#5#5#{interval}#M$M#signature'}}


def frame():
    rng = np.random.default_rng(4)
    return Image.fromarray(rng.integers(10, 240, (90, 160, 3), dtype=np.uint8))


def sheet(second=None):
    image = Image.new('RGB', (800, 450))
    image.paste(frame(), (640, 0))  # 20s: cell 4
    image.paste(second or frame(), (320, 180))  # 60s: cell 12
    return image


def test_storyboard_samples_are_20s_and_60s_in_the_correct_cells_and_sheets():
    selected = tv_visual.samples(storyboard(), 'visual00002', 200)
    assert [s['at'] for s in selected] == [20, 60]
    assert [s['box'] for s in selected] == [(640, 0, 800, 90), (320, 180, 480, 270)]
    assert all('/M0.jpg?' in s['url'] and 'sigh=signature' in s['url'] for s in selected)
    selected = tv_visual.samples(storyboard(interval=1000, count=100), 'visual00002', 200)
    assert '/M0.jpg?' in selected[0]['url'] and '/M2.jpg?' in selected[1]['url']


@pytest.mark.parametrize('data,duration', [
    ({}, 200), (storyboard(), 60), (storyboard(interval=0), 200),
    (storyboard(interval=20000), 200), (storyboard(count=10), 200),
    (storyboard('visual00003'), 200), ({'playerStoryboardSpecRenderer': {'spec': 'bad'}}, 200),
    ({'playerStoryboardSpecRenderer': {'spec':
        storyboard()['playerStoryboardSpecRenderer']['spec'].replace('160#90#', '80#45#')}}, 200),
])
def test_missing_short_coarse_or_wrong_video_previews_are_unknown(data, duration):
    assert tv_visual.samples(data, 'visual00002', duration) == []


def test_comparison_rejects_stills_and_tolerates_small_compression_noise_and_logo():
    original = frame()
    assert tv_visual.compare([original, original])['verdict'] == 'static'
    noisy = Image.fromarray((np.asarray(original).astype(np.int16) + 1).clip(0, 255).astype(np.uint8))
    assert tv_visual.compare([original, noisy])['verdict'] == 'static'
    logo = original.copy()
    logo.paste((255, 0, 0), (0, 0, 8, 6))
    assert tv_visual.compare([original, logo])['verdict'] == 'static'
    changed = original.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    assert tv_visual.compare([original, changed])['verdict'] == 'moving'
    assert tv_visual.compare([Image.new('RGB', (160, 90), 'red')] * 2)['verdict'] == 'unknown'


def test_moderat_artwork_uses_adequate_previews_and_is_static(store, monkeypatch):
    fixtures = Path(__file__).parent / 'fixtures' / 'tv_visual'
    frames = [Image.open(fixtures / f'moderat-{seconds}s.png') for seconds in (20, 60)]
    data = {'playerStoryboardSpecRenderer': {'spec':
        'https://i.ytimg.com/sb/nGMtAQb63x8/storyboard3_L$L/$N.jpg'
        '|48#27#100#10#10#0#default#signature'
        '|80#45#67#10#10#5000#M$M#signature'
        '|160#90#67#5#5#5000#M$M#signature'}}
    selected = tv_visual.samples(data, 'nGMtAQb63x8', 335)
    assert len(selected) == 2 and all('/storyboard3_L2/' in s['url'] for s in selected)
    image = Image.new('RGB', (800, 450))
    for frame, sample in zip(frames, selected):
        image.paste(frame, sample['box'][:2])
    monkeypatch.setattr(tv_visual, '_read_image', lambda *_: image)
    client = SimpleNamespace(get_video_storyboard=lambda _: data)
    result = tv_visual.Inspector().inspect(store, client, {**video(2), 'video_id': 'nGMtAQb63x8'}, 1)
    assert result['verdict'] == 'static' and result['sample_times'] == [20, 60]
    assert 'nGMtAQb63x8' in tv_visual.static_ids(store)


def test_old_low_resolution_moving_verdict_is_rechecked(store):
    store.rec_proposals.put_proposals(tv_visual.SURFACE, {
        'nGMtAQb63x8': {'version': 1, 'verdict': 'moving', 'at': 1}}, 1)
    assert tv_visual.cached(store, 'nGMtAQb63x8', 2) is None


def test_inspector_fetches_one_sheet_caches_static_and_excludes_it_from_tv(store, monkeypatch):
    calls = []
    def read(url, deadline):
        calls.append(url)
        return sheet()
    monkeypatch.setattr(tv_visual, '_read_image', read)
    client = SimpleNamespace(get_video_storyboard=lambda video_id: storyboard(video_id))
    item = video(2)
    inspector = tv_visual.Inspector()
    result = inspector.inspect(store, client, item, 1)
    assert result['verdict'] == 'static' and result['sample_times'] == [20, 60]
    assert len(calls) == 1
    assert inspector.inspect(store, None, item, 10000) == result
    assert len(calls) == 1
    assert tv.eligible(store, [item], None, 2) == []
    assert not store.history.play_events_since(0)
    assert not store.rec_model.suppressed_keys('for_you', 2), 'Static is a video property, not musical feedback'


@pytest.mark.parametrize('failure', ['network', 'truncated', 'missing', 'short', 'decoder'])
def test_inconclusive_analysis_is_unknown_and_retryable(store, monkeypatch, failure):
    def read(*args):
        if failure == 'truncated':
            return Image.new('RGB', (10, 10))
        if failure == 'decoder':
            raise Image.DecompressionBombError('bad image')
        raise TimeoutError('no image')
    monkeypatch.setattr(tv_visual, '_read_image', read)
    item = video(2, duration=50 if failure == 'short' else 200)
    client = SimpleNamespace(get_video_storyboard=lambda _: {} if failure == 'missing' else storyboard())
    result = tv_visual.Inspector().inspect(store, client, item, 1)
    assert result['verdict'] == 'unknown'
    assert tv_visual.cached(store, item['video_id'], 2) == result
    assert tv_visual.cached(store, item['video_id'], 301) is None
    assert tv.eligible(store, [item], None, 2) == [item]


def test_concurrent_inspection_returns_pending_without_a_second_network_job(store):
    inspector = tv_visual.Inspector()
    inspector.lock.acquire()
    try:
        assert inspector.inspect(store, None, video(2), 1) is None
    finally:
        inspector.lock.release()


@pytest.mark.parametrize('kind', ['valid', 'redirect', 'html', 'oversize', 'corrupt', 'deadline'])
def test_image_reader_decodes_bounded_images_and_refuses_unusable_downloads(monkeypatch, kind):
    encoded = BytesIO()
    sheet().save(encoded, format='JPEG')
    payload = b'bad JPEG' if kind == 'corrupt' else encoded.getvalue()
    from yt_playlist.integrations.youtube_music import YouTubeStoryboardIAO
    access = YouTubeStoryboardIAO()
    monkeypatch.setattr(tv_visual, 'storyboards', access)
    if kind == 'oversize':
        monkeypatch.setattr(access, 'MAX_BYTES', 100)
    calls = []
    class Response:
        status_code = 302 if kind == 'redirect' else 200
        headers = {'Content-Type': 'text/html' if kind == 'html' else 'image/jpeg'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield payload
    def get(url, **options):
        calls.append(options)
        return Response()
    access.get = get
    deadline = time.monotonic() + (-1 if kind == 'deadline' else 10)
    url = 'https://i.ytimg.com/sb/abcdefghijk/storyboard3_L1/M0.jpg?sigh=signed'
    if kind == 'valid':
        assert tv_visual._read_image(url, deadline).size == (800, 450)
    else:
        with pytest.raises((ValueError, OSError, TimeoutError)):
            tv_visual._read_image(url, deadline)
    if calls:
        assert calls[0]['allow_redirects'] is False and calls[0]['stream'] is True


def test_bridge_keeps_storyboards_which_get_song_would_strip(monkeypatch):
    calls = []
    def send(self, endpoint, body):
        calls.append((endpoint, body))
        return {'videoDetails': {'videoId': body['videoId']}, 'storyboards': storyboard()}
    monkeypatch.setattr(BridgeClient, '_send_request', send)
    client = object.__new__(BridgeClient)
    assert client.get_video_storyboard('visual00002') == storyboard()
    assert calls == [('player', {'videoId': 'visual00002'})]
    monkeypatch.setattr(BridgeClient, '_send_request', lambda *args: {'videoDetails': {'videoId': 'other'}})
    assert client.get_video_storyboard('visual00002') == {}


def test_visual_route_checks_membership_and_uses_the_playlist_account(store, stream, monkeypatch):
    iid, client, playlist, recipe, items = stream
    other = store.identities.upsert_identity('other', 'cred2', None, False)
    calls = []
    def inspect(self, actual_store, actual_client, item, now):
        calls.append((actual_client, item['video_id']))
        return {'verdict': 'moving', 'sample_times': [20, 60]}
    monkeypatch.setattr(tv_visual.Inspector, 'inspect', inspect)
    with TestClient(create_app(store, lambda: {iid: client, other: object()}), base_url='http://127.0.0.1') as web:
        url = f'/home/tv/playlist/{playlist.id}/visual-check'
        result = web.post(url, data={'video_id': items[2]['video_id']})
        assert result.json() == {'video_id': items[2]['video_id'], 'verdict': 'moving', 'sample_times': [20, 60]}
        assert calls == [(client, items[2]['video_id'])]
        assert web.post(url, data={'video_id': 'visual00059'}).status_code == 400
        assert web.post('/home/tv/playlist/999/visual-check', data={'video_id': 'visual00002'}).status_code == 404
        assert len(calls) == 1


@pytest.mark.parametrize('method,url,body,allowed', [
    ('GET', 'https://i.ytimg.com/sb/visual00002/storyboard3_L0/M0.jpg?sigh=secret', None, True),
    ('POST', 'https://i.ytimg.com/sb/visual00002/storyboard3_L0/M0.jpg', None, False),
    ('GET', 'https://i.ytimg.com/sb/visual00002/storyboard3_L0/M0.jpg', b'body', False),
    ('GET', 'https://i.ytimg.com/vi/visual00002/hqdefault.jpg', None, False),
    ('GET', 'https://i.ytimg.com.evil.com/sb/visual00002/storyboard3_L0/M0.jpg', None, False),
    ('GET', 'http://i.ytimg.com/sb/visual00002/storyboard3_L0/M0.jpg', None, False),
    ('GET', 'https://user:secret@i.ytimg.com/sb/visual00002/storyboard3_L0/M0.jpg', None, False),
    ('GET', 'https://i.ytimg.com:444/sb/visual00002/storyboard3_L0/M0.jpg', None, False),
])
def test_only_bodyless_https_timeline_gets_cross_the_image_egress_boundary(tmp_path, method, url, body, allowed):
    assert not host_allowed('i.ytimg.com')
    guard = EgressGuard(tmp_path / 'network.log')
    if allowed:
        assert guard.gate(url, method=method, body=body, via='requests')[0] == 'i.ytimg.com'
    else:
        with pytest.raises(BlockedHost):
            guard.gate(url, method=method, body=body, via='requests')
    assert 'secret' not in '\n'.join(guard.recent())
