"""Controlled current recommendation publications, without retired cache writes."""
from tests.conftest import seed_catalog_track
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.repos import recommendation_publication as publication


def plan(store, key, *, count=2, policy='fixture', value=None):
    if store.rec_ledger.policy_configuration(policy) is None:
        store.rec_ledger.record_serving_policy(policy, {'ranker': policy}, 1)
    subjects = []
    for n in range(count):
        recording = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (f'staged-{n}',)).fetchone()
        identity = recording[0] if recording else seed_catalog_track(store, f'staged-{n}', f'Café {n}',
            f'Artist {n}', video_id=f'staged-video-{n}')
        subjects.append(pools.Candidate(identity, n / max(count, 1)))
    pool = pools.prepare('fixture', policy, None, 'a' * 64, 'card', subjects)
    body = {'title': key} if value is None else value
    return publication.Plan(key, policy, None, 1, 10,
        (publication.Slot('home', 'all', 'wheelhouse', pool),
         publication.Slot('fresh', 'all', 'fresh', pool)),
        (publication.Output('home', 'fixture', outputs.encode('home', 'fixture', body)),),
        publication.Framing(presence=1))


def stage(store, prepared):
    build = store.recommendations.claim(prepared)
    store.recommendations.stage(prepared, build)
    return build


def publish(store, prepared):
    build = stage(store, prepared)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(build, 11)
    return build
