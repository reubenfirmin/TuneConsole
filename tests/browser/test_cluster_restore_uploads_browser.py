"""Restored Clusters canvases use the recordings' current playable uploads."""
import pytest
from playwright.sync_api import expect

from tests.test_clusters_route import mark_cluster_upload_unavailable, seed_cluster_discoveries


DATA = "Alpine.$data(document.querySelector('.cluster-page'))"


@pytest.mark.parametrize('use_alternate', [False, True], ids=['omit', 'alternate'])
def test_restored_canvas_refreshes_playback_without_changing_graph(live_app, store, page, use_alternate):
    seed_cluster_discoveries(store)
    page.set_viewport_size({'width': 1600, 'height': 1050})
    page.goto(f'{live_app}/clusters')
    page.get_by_role('textbox', name='Add artists, playlists, or songs to the central cluster').fill('Seed')
    page.locator('.cluster-suggest li:has(.cs-kind.song)').filter(has_text='Seed').first.click()
    page.wait_for_function(f"{DATA}.nodes.some(node => node.vid === 'first-upload')")
    page.get_by_title('DJ Journey: how the mix is ordered').click()
    page.locator('.cj-item:has([data-flow="shuffle"])').click()
    graph = f"{DATA}.nodes.map(node => [node.id, node.parentId, node.key, node.state])"
    before = page.evaluate(graph)
    node_id = page.evaluate(f"{DATA}.nodes.find(node => node.vid === 'first-upload').id")
    page.wait_for_function(f"!{DATA}._autofit && {DATA}._fg.zoom() <= 1.21")
    page.evaluate(f'{DATA}._flushState()')
    mark_cluster_upload_unavailable(store, use_alternate)

    played = []

    def play(route):
        played.append(route.request.post_data_json['url'])
        route.fulfill(json={'ok': True})

    page.route('**/play', play)
    with page.expect_response('**/clusters/uploads') as lookup:
        page.reload()
    assert lookup.value.status == 200
    page.wait_for_function(f"{DATA}.nodes.some(node => node.vid === 'second-upload') && !{DATA}._pendingView")
    assert page.evaluate(graph) == before
    assert page.evaluate(f'{DATA}.journey') == 'shuffle'
    point = page.evaluate(f"""() => {{
        const app = {DATA}, node = app.nodeById({node_id});
        const point = app._fg.graph2ScreenCoords(node.x, node.y);
        const canvas = document.querySelector('.cluster-graph canvas').getBoundingClientRect();
        return {{x: point.x + canvas.left, y: point.y + canvas.top}};
    }}""")
    page.mouse.move(point['x'], point['y'])
    page.wait_for_function(f'{DATA}.hovered === {node_id}')
    button = page.get_by_title('Play on YouTube Music')
    if use_alternate:
        with page.expect_response('**/play'):
            button.click()
        assert played == ['https://music.youtube.com/watch?v=alternate-upload']
    else:
        expect(button).to_be_hidden()
        assert not played
