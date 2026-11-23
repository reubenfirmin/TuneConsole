from fastapi.testclient import TestClient
from tests.conftest import FakeClient, seed_catalog_track
from tests.group_support import publish_keyed
from yt_playlist.rec import group_serving
from yt_playlist.web.app import create_app


def _client(store, now=10_000.0):
    iid = store.accounts.upsert_account("main", None, True)
    fake = FakeClient()
    return TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def test_taste_profile_links_to_published_music_groups(store):
    keys = [f'house {index}|artist {index}' for index in range(8)]
    for index, key in enumerate(keys):
        seed_catalog_track(store, key, f'House {index}', f'Artist {index}',
                           video_id=f'house-{index}', genre='House')
    publish_keyed(store, [('House', keys, .8)], now=9_000.0)
    generation = store.taste_groups.head_generation()
    groups = store.taste_groups.definitions(generation)
    leaf, = (group for group in groups if group['leaf'])
    assert group_serving.prepare(store, 9_000.0) is not None
    with _client(store) as c:
        profile = c.get("/taste/panels/profile")
        assert profile.status_code == 200
        assert '<dt>Listening styles</dt><dd>1</dd>' in profile.text
        assert 'href="#groups"' in profile.text and 'Explore music groups' in profile.text
        group_panel = c.get("/taste/panels/groups")
        assert group_panel.status_code == 200
        assert f'data-generation="{generation}"' in group_panel.text
        assert f'data-group-version="{leaf["version"]}"' in group_panel.text
        assert f'<h3>{leaf["name"]}</h3>' in group_panel.text
        assert '8 recordings grouped' in group_panel.text


def test_taste_page_empty_modes_state(store):
    c = _client(store)
    r = c.get("/taste/panels/profile")
    assert r.status_code == 200
    assert "No listening styles found yet" in r.text
