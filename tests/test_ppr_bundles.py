from dataclasses import replace

from yt_playlist.rec import surfaces, ppr, mode_surfaces as ms
from tests.conftest import seed_catalog_track
from tests.group_support import publish, recordings


def seed_items(store, keys):
    for key in keys:
        seed_catalog_track(store, key, 'T' + key, 'A' + key, video_id='v' + key, genre='House')


class _Item:
    def __init__(self, key):
        self.key = key; self.video_id = "v" + key; self.title = "T" + key; self.artist = "A" + key
        self.album = ""; self.thumbnail = None; self.plays = 0; self.reason = ""; self.lane = ""
        self.genre = "house"


def test_prepare_stashes_ppr_order(monkeypatch, store):
    keys = ["k1", "k2"]
    seed_items(store, keys)
    corpus = recordings([('House', 1, .1), ('Metal', 1, .9)])
    head = publish(store, [replace(track, key=key) for track, key in zip(corpus, keys)])
    membership = store.taste_groups.leaf_members(head['generation'])
    assert len(set(membership.values())) == 2
    monkeypatch.setattr(ppr, 'build_transition', lambda s: ppr.transition_from_baskets([keys]))
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit=0, **kw: [_Item("k1"), _Item("k2")])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None, **kw: [])
    payload = ms.prepare_bundles(store, now=10.0)
    assert payload['_meta']['group_generation'] == head['generation']
    assert not store.modes.list_modes()
    for key, group in membership.items():
        assert [row['key'] for row in payload[group]['wheelhouse']] == [key]
        assert payload['_radio']['members'][group] == [key]
        expected = [key, *[other for other in keys if other != key]]
        assert payload['_ppr'][group] == expected
        assert [row['key'] for row in store.recommendations.page('radio', group, 'ppr')] == expected


def test_prepare_ppr_failopen(monkeypatch, store):
    seed_items(store, ['k1'])
    track, = recordings([('House', 1, .1)])
    head = publish(store, [replace(track, key='k1')])
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit=0, **kw: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None, **kw: [])

    def _boom(s, **kw):
        raise RuntimeError("ppr blew up")
    monkeypatch.setattr(ppr, "build_transition", _boom)

    payload = ms.prepare_bundles(store, now=10.0)      # must not raise
    assert payload["_ppr"] == {}
    group, = store.taste_groups.leaf_members(head['generation']).values()
    assert payload['_radio']['members'][group] == ['k1']
