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


def test_failure_without_saved_card_is_short_lived_and_not_a_miss(store):
    subject = "artist:offline"
    store.wiki.defer(subject, 1000)
    assert store.wiki.get(subject) is None
    assert not store.wiki.should_refresh(subject, None, 1059)
    assert store.wiki.should_refresh(subject, None, 1060)


def test_demand_backoff_respects_a_later_vendor_deadline_without_restamping_content(store):
    subject = 'artist:offline'
    store.wiki.put(subject, 'artist', 'Offline', {'title': 'Offline', 'extract': 'Old card.'}, now=0)
    row = store.wiki.get(subject)
    now = 31 * 86400
    store.wiki.defer(subject, now, retry_at=now + 600)
    assert not store.wiki.should_refresh(subject, row, now + 599)
    assert store.wiki.should_refresh(subject, row, now + 600)
    assert store.wiki.get(subject)['fetched_at'] == 0


def test_saved_cards_and_failure_backoffs_are_bounded(store, monkeypatch):
    from yt_playlist.repos import wiki
    monkeypatch.setattr(wiki, "WIKI_CACHE_LIMIT", 3)
    for i in range(5):
        subject = f"artist:{i}"
        store.wiki.defer(subject, i)
        store.wiki.put(subject, "artist", str(i), {"extract": "A band."}, i)
    assert store.wiki.get("artist:0") is None
    assert store.wiki.get("artist:4")["extract"] == "A band."
    assert store.conn.execute("SELECT COUNT(*) FROM wiki_cards").fetchone()[0] == 3
    for i in range(5):
        store.wiki.defer(f"artist:{i}", i)
    assert len(store.wiki._retry_after) == 3
