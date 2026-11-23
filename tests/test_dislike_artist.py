"""#54: disliking one song by an artist must not suppress all of that artist's songs."""
from tests.conftest import seed_catalog_track
from tests.retained_rating_support import observe_rating
from yt_playlist.rec import transient


def test_dislike_is_scoped_to_the_rated_recording(store):
    seed_catalog_track(store, 'rebel yell|billy idol', 'Rebel Yell', 'Billy Idol',
                       video_id='rebel-yell', genre='rock-classic', mb_year='1983')
    seed_catalog_track(store, 'eyes without a face|billy idol', 'Eyes Without a Face', 'Billy Idol',
                       video_id='eyes-without-a-face', genre='rock-classic', mb_year='1983')
    observe_rating(store, 'rebel yell|billy idol', 'DISLIKE', 100., provenance='api')
    assert store.rec_model.disliked_with_ts() == [('rebel yell|billy idol', 100.)]
    assert store.rec_model.suppressed_keys('for_you', now=100.) == {'rebel yell|billy idol'}
    assert transient.facet_leans(store, now=100.) == {}
    assert store.rec_model.get_weights(now=100.) == {}
