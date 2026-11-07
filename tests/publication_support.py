"""Controlled current recommendation publications, without retired cache writes."""
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.repos import recommendation_publication as publication


def _subjects(store, count):
    """Seed catalog membership in one transaction for publication contracts.

    These tests own candidate staging, cutover and retirement. Artist-credit
    observation and catalog ingestion have separate owners; neither is needed
    to construct their existing playable subjects.
    """
    existing = dict(store.conn.execute(
        "SELECT identity_key,id FROM recordings WHERE identity_key LIKE 'staged-%'"))
    first_recording = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM recordings').fetchone()[0]
    first_entry = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM catalog_entries').fetchone()[0]
    missing = [n for n in range(count) if f'staged-{n}' not in existing]
    rows = [(first_recording + offset, first_entry + offset, f'staged-{n}',
             f'staged-video-{n}', f'Café {n}', f'Artist {n}') for offset, n in enumerate(missing)]
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            [(rid, key, title, artist) for rid, _, key, _, title, artist in rows])
        store.conn.executemany('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) '
            'VALUES (?,?,?,?,?,?)', [(video, rid, title, artist, title, artist)
                                   for rid, _, _, video, title, artist in rows])
        store.conn.executemany('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)',
            [(entry, rid, video) for rid, entry, _, video, _, _ in rows])
        store.conn.executemany("INSERT INTO library_memberships VALUES (?,'fixture',NULL)",
            [(entry,) for _, entry, _, _, _, _ in rows])
    existing.update((key, rid) for rid, _, key, _, _, _ in rows)
    return [pools.Candidate(existing[f'staged-{n}'], n / max(count, 1)) for n in range(count)]


def plan(store, key, *, count=2, policy='fixture', value=None):
    if store.rec_ledger.policy_configuration(policy) is None:
        store.rec_ledger.record_serving_policy(policy, {'ranker': policy}, 1)
    subjects = _subjects(store, count)
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
