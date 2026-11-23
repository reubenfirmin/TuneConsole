from operator import attrgetter

import pytest

def test_put_get_hit_roundtrip(store):
    store.wiki.repository.put("artist:khruangbin", "artist", "Khruangbin",
                   {"title": "Khruangbin", "extract": "A band.",
                    "thumbnail": "http://img", "url": "http://wiki"}, now=1000.0, found=True, limit=512)
    row = store.wiki.repository.get("artist:khruangbin")
    assert row["found"] == 1
    assert row["display"] == "Khruangbin"
    assert row["extract"] == "A band."
    assert row["thumbnail"] == "http://img"


def test_negative_cache_roundtrip(store):
    store.wiki.repository.put("genre:nonsense", "genre", "nonsense", {}, now=1000.0,
                              found=False, limit=512)
    row = store.wiki.repository.get("genre:nonsense")
    assert row["found"] == 0
    assert row["extract"] is None


def test_get_absent_returns_none(store):
    assert store.wiki.repository.get("artist:nobody") is None


@pytest.mark.parametrize('reader, get_store', [
    ('read_view', lambda view: view),
    ('read_snapshot', attrgetter('store')),
])
def test_memory_reader_keeps_captured_wikipedia_cards(store, reader, get_store):
    subject = 'artist:hit'
    wiki = store.wiki.repository
    wiki.put(subject, 'artist', 'Hit', {'extract': 'Original'}, 100, found=True, limit=512)
    with getattr(store, reader)() as captured:
        captured_wiki = get_store(captured).wiki.repository
        assert captured_wiki.get(subject)['extract'] == 'Original'
        wiki.put(subject, 'artist', 'Hit', {'extract': 'Updated'}, 200, found=True, limit=512)
        assert captured_wiki.get(subject)['extract'] == 'Original'
    with getattr(store, reader)() as captured:
        assert get_store(captured).wiki.repository.get(subject)['extract'] == 'Updated'


def test_write_uses_supplied_bound_and_deterministic_timestamp_order(store):
    for i in range(5):
        subject = f"artist:{i}"
        store.wiki.repository.put(subject, "artist", str(i), {"extract": "A band."}, i,
                                  found=True, limit=3)
    store.wiki.repository.put("artist:1", "artist", "1", {}, 2, found=False, limit=3)
    assert [i for i in range(5) if store.wiki.repository.get(f"artist:{i}")] == [1, 3, 4]
    assert store.wiki.repository.get("artist:4")["extract"] == "A band."
    assert not store.conn.execute("SELECT 1 FROM settings WHERE key GLOB 'wiki_card:*'").fetchone()
