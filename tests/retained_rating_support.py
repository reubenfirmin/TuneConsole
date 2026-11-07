"""Create account observations or project combined evidence for rating tests."""
from yt_playlist.core.rating_feedback import Rating
from yt_playlist.library import ratings
from yt_playlist.rec import graduation


def apply_model_ratings(store, statuses, now, provenance='sync'):
    """Exercise the model projection with explicit, already combined evidence."""
    graduation.apply_ratings(store, {key: Rating(status, now, provenance)
                                    for key, status in statuses.items()})


def observe_rating(store, key, status, now, *, provenance='sync'):
    masters = [account for account in store.accounts.get_accounts() if account.is_master]
    account = masters[0].id if masters else store.accounts.upsert_account('rating fixture', 'fixture', None, True)
    row = store.conn.execute('SELECT video_id FROM tracks WHERE identity_key=? LIMIT 1', (key,)).fetchone()
    if row is None:
        title, artist = key.split('|', 1)
        video = 'rating-fixture:' + key
        store.tracks.upsert_track(video, title, artist, '', None)
    else:
        video = row['video_id']
    ratings.record_observations(store, account, {video: status}, source=provenance, now=now)
