from tests.retained_rating_support import observe_rating
from yt_playlist.rec import rec_params


def test_retained_dislike_idempotent_and_global(store):
    observe_rating(store, 'song|artist', 'DISLIKE', 1000)
    observe_rating(store, 'song|artist', 'DISLIKE', 1001)
    assert store.rec_model.disliked_with_ts() == [('song|artist', 1000)]
    assert store.rec_model.disliked_identity_keys() == {"song|artist"}
    assert "song|artist" in store.rec_model.suppressed_keys("for_you", 1000)
    assert "song|artist" in store.rec_model.suppressed_keys("suggest", 1000, scope="42")


def test_dislike_until_recycles(store):
    rec_params.set_param(store, 'dislike_suppress_days', 1)
    observe_rating(store, 'k|a', 'DISLIKE', 1000)
    assert store.rec_model.list_dislikes()[0]['until'] == 1000 + 86400
    assert "k|a" in store.rec_model.suppressed_keys("for_you", 1000 + 86400 - 1)
    assert "k|a" not in store.rec_model.suppressed_keys("for_you", 1000 + 86400 + 1)


def test_retained_neutral_clears_dislike(store):
    observe_rating(store, 'k|a', 'DISLIKE', 1000)
    observe_rating(store, 'k|a', 'INDIFFERENT', 1001)
    assert store.rec_model.disliked_identity_keys() == set()
    assert "k|a" not in store.rec_model.suppressed_keys("for_you", 1000)
