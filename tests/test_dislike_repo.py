def test_record_dislike_idempotent_and_global(store):
    assert store.record_dislike("song|artist", until=1000 + 365 * 86400, now=1000) is True
    assert store.record_dislike("song|artist", until=1000 + 365 * 86400, now=1000) is False
    assert store.disliked_identity_keys() == {"song|artist"}
    assert "song|artist" in store.suppressed_keys("for_you", 1000)
    assert "song|artist" in store.suppressed_keys("suggest", 1000, scope="42")


def test_dislike_until_recycles(store):
    store.record_dislike("k|a", until=2000, now=1000)
    assert "k|a" in store.suppressed_keys("for_you", 1999)
    assert "k|a" not in store.suppressed_keys("for_you", 2001)


def test_clear_dislike(store):
    store.record_dislike("k|a", until=9e9, now=1000)
    store.clear_dislike("k|a")
    assert store.disliked_identity_keys() == set()
    assert "k|a" not in store.suppressed_keys("for_you", 1000)
