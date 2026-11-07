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


def test_write_uses_supplied_bound_and_deterministic_timestamp_order(store):
    for i in range(5):
        subject = f"artist:{i}"
        store.wiki.repository.put(subject, "artist", str(i), {"extract": "A band."}, i,
                                  found=True, limit=3)
    assert store.wiki.repository.get("artist:0") is None
    assert store.wiki.repository.get("artist:4")["extract"] == "A band."
    assert store.conn.execute("SELECT COUNT(*) FROM wiki_cards").fetchone()[0] == 3
