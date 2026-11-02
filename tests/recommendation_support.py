"""Publish controlled candidate rankings through the ordinary current owner."""
from copy import deepcopy

from tests.conftest import seed_catalog_track
from yt_playlist.rec import measurement, mode_surfaces


def seed_bundle_subjects(store, bundle, *, fresh=()):
    """Give every playable fixture row its actual library/discovery owner."""
    tracks = {}
    for mode, bucket in bundle.items():
        if mode.startswith('_'):
            continue
        for lane, items in bucket.items():
            for item in items:
                if item.get('key') and 'title' in item and 'artist' in item:
                    previous = tracks.get(item['key'])
                    tracks[item['key']] = (lane != 'fresh' or bool(previous and previous[0]), item)
    for item in fresh:
        tracks.setdefault(item['key'], (False, item))
    owned = store.rec_query.tracks_by_keys(tracks)
    discovered = store.discovery.discovered_tracks_by_keys(tracks)
    for key, (library, item) in tracks.items():
        if key in owned or key in discovered:
            continue
        video = item.get('video_id') or 'fixture-' + key
        existing = store.conn.execute('SELECT r.identity_key FROM youtube_items y JOIN recordings r '
                                      'ON r.id=y.recording_id WHERE y.video_id=?', (video,)).fetchone()
        if existing and existing[0] != key:
            video = 'fixture-' + key
        if library:
            seed_catalog_track(store, key, item['title'], item['artist'], video_id=video,
                               album=item.get('album'), genre=item.get('genre'))
        else:
            store.discovery.upsert_discovered_track(key, video, item['title'], item['artist'],
                item.get('album'), item.get('thumbnail'), item.get('genre'), item.get('year'), None, 1)


def publish_bundles(store, bundle, now=1000, *, fresh=(), vibes=False):
    with store.read_snapshot() as snapshot:
        policies = measurement.PoolPolicies(snapshot.store, now)
        if vibes:
            from yt_playlist.rec import vibe_pools
            vibe_pools.prepare(snapshot.store, bundle, now, policies)
        prepared = {key: value if key.startswith('_') else {
            lane: policies.stamp(items, lane) for lane, items in value.items()}
            for key, value in bundle.items()}
        if '_vibes' in prepared:
            selected = deepcopy(prepared['_vibes'])
            selected['pools'] = {preset: {lane: policies.stamp(items, 'vibe:' + preset)
                for lane, items in lanes.items()} for preset, lanes in selected.get('pools', {}).items()}
            selected['genres'] = {preset: {lane: {genre: policies.stamp(items, 'vibe:' + preset)
                for genre, items in groups.items()} for lane, groups in lanes.items()}
                for preset, lanes in selected.get('genres', {}).items()}
            prepared['_vibes'] = selected
        generation = mode_surfaces._publish_typed_bundles(store, snapshot, prepared,
            policies.stamp(list(fresh), 'fresh'), policies, {}, now)
    assert generation is not None
    store._fixture_bundle_payload = deepcopy(bundle)
    return generation
