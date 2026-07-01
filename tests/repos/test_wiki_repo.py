def test_put_get_hit_roundtrip(store):
    store.wiki.put("artist:khruangbin", "artist", "Khruangbin",
                   {"title": "Khruangbin", "extract": "A band.",
                    "thumbnail": "http://img", "url": "http://wiki"}, now=1000.0)
    row = store.wiki.get("artist:khruangbin")
    assert row["found"] == 1
    assert row["display"] == "Khruangbin"
    assert row["extract"] == "A band."
    assert row["thumbnail"] == "http://img"
    assert store.wiki.is_fresh(row, now=1000.0 + 5 * 86400) is True


def test_negative_cache_and_miss_ttl(store):
    store.wiki.put("genre:nonsense", "genre", "nonsense", None, now=1000.0)
    row = store.wiki.get("genre:nonsense")
    assert row["found"] == 0
    assert row["extract"] is None
    assert store.wiki.is_fresh(row, now=1000.0 + 6 * 86400) is True
    assert store.wiki.is_fresh(row, now=1000.0 + 8 * 86400) is False


def test_hit_ttl_expires(store):
    store.wiki.put("genre:shoegaze", "genre", "shoegaze",
                   {"title": "Shoegaze", "extract": "A genre.", "thumbnail": None,
                    "url": "http://wiki"}, now=0.0)
    row = store.wiki.get("genre:shoegaze")
    assert store.wiki.is_fresh(row, now=29 * 86400) is True
    assert store.wiki.is_fresh(row, now=31 * 86400) is False


def test_get_absent_returns_none(store):
    assert store.wiki.get("artist:nobody") is None
